"""The restart repair's test app: a writer whose write tool asks for approval, on a real session
stack with one provider key; a crash of that app (an observer or a write that raises
CancelledError, as a process dying there would leave it); and its restart, a new approval store,
sessions and approvals service over the same databases, as a process start builds them. A
crashed app is dropped, never closed: a process death closes nothing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx2
from sqlalchemy import func, select

from mlops_agents.agents.approvals.contract import ApprovalView
from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.service import ApprovalsService
from mlops_agents.agents.approvals.store import ApprovalRecord, ApprovalStore
from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.policy.table import PermissionLevel
from mlops_agents.agents.runtime.contract import (
    ApprovalPart,
    MessageView,
    StreamEvent,
    TurnEnd,
    TurnEnded,
    TurnWaiting,
)
from mlops_agents.agents.runtime.hooks import TurnObserver
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools.base import RiskClass
from mlops_agents.config import RunMode, load_settings
from mlops_agents.events.audit import SqliteAuditLog
from mlops_agents.events.contract import AuditDecision, AuditRecord
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.failover import KeyFailover
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import audit, events
from mlops_agents.workspace.service import WorkspaceService
from tests.agents.approval_env import TOOL_PREVIEW, RecordingObserver, add_turn
from tests.agents.runtime.hook_fakes import NoInbound
from tests.agents.runtime.session_env import collect_until_turn_ended, opened_stream
from tests.agents.runtime.stall_env import KEY, StallEnv, build_stall_env
from tests.agents.runtime.test_resuming_turns import (
    DummyWriteTool,
    collect_until_turn_waiting,
    make_tool_reg,
    write_agent_def,
)
from tests.conftest import STAGE_2_TOOLS, MemoryKeyStore, memory_project_secrets
from tests.fakes.fake_openai import RunningFake
from tests.fakes.turn_end import NoTurnEndCheck
from tests.fakes.workspaces import FakeWorkspaces
from tests.integrations.test_github_auth import FakeGh
from tests.providers.fake_clock import FakeClock

WRITER = "writer"
QUIET = "quiet"  # an agent with chat: false
Decision = Literal["approve", "reject", "cancel"]


@dataclass(frozen=True)
class App:
    """One process's approvals wiring: its gate, manager and service share `store`, on which
    `observer` records every call."""

    manager: SessionManager
    store: ApprovalStore
    service: ApprovalsService
    observer: RecordingObserver


def observed(store: ApprovalStore) -> RecordingObserver:
    observer = RecordingObserver()
    store.add_observer(observer)
    return observer


class CrashOnDecided:
    """The process dies while the observers are told of a decision: the first `decided` raises
    CancelledError (a plain Exception would be caught and logged, and the decision go on)."""

    async def asked(self, view: ApprovalView) -> None:
        return None

    async def decided(self, view: ApprovalView) -> None:
        raise asyncio.CancelledError


class TurnCounter(TurnObserver):
    """Records each turn's waits and ends, as (turn ID, status)."""

    def __init__(self) -> None:
        self.waited: list[str] = []
        self.ended: list[tuple[str, str]] = []
        self._changed = asyncio.Event()

    async def turn_started(self, project_id: str, agent: str, turn_id: str) -> None:
        return None

    async def turn_waiting(self, project_id: str, agent: str, turn_id: str) -> None:
        self.waited.append(turn_id)
        self._changed.set()

    async def turn_ended(
        self, project_id: str, agent: str, turn_id: str, status: TurnEnd, final_text: str | None
    ) -> None:
        self.ended.append((turn_id, status))
        self._changed.set()

    async def wait_ended(self, turn_id: str) -> None:
        """Until the turn's end is reported to the observers (after its stream event)."""
        async with asyncio.timeout(10):
            while all(t != turn_id for t, _ in self.ended):
                self._changed.clear()
                await self._changed.wait()


class CancelledOnceAuditLog(SqliteAuditLog):
    """Its first write is cancelled, as a process dying there would leave it."""

    def __init__(self, databases: Databases) -> None:
        super().__init__(databases, secrets=[])
        self.calls = 0

    async def record(
        self,
        project_id: str,
        *,
        actor: str,
        action: str,
        risk_class: RiskClass,
        permission_level: PermissionLevel,
        decision: AuditDecision,
        target: str,
        detail: dict[str, Any],
    ) -> AuditRecord:
        self.calls += 1
        if self.calls == 1:
            raise asyncio.CancelledError
        return await super().record(
            project_id,
            actor=actor,
            action=action,
            risk_class=risk_class,
            permission_level=permission_level,
            decision=decision,
            target=target,
            detail=detail,
        )


def cancelled_once(call: Callable[..., Awaitable[object]]) -> Callable[..., Awaitable[object]]:
    """`call`, whose first call is cancelled, as a process dying there would leave it."""
    calls = 0

    async def wrapped(*args: Any, **kwargs: Any) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError
        return await call(*args, **kwargs)

    return wrapped


class HeldObserver:
    """Holds `decided` for each listed approval until its release, as a slow proposal `apply`
    would; records which calls finished and which were cancelled."""

    def __init__(self, approval_ids: Iterable[str]) -> None:
        ids = list(approval_ids)
        self.entered = {i: asyncio.Event() for i in ids}
        self.release = {i: asyncio.Event() for i in ids}
        self.finished: list[str] = []
        self.cancelled: list[str] = []

    async def asked(self, view: ApprovalView) -> None:
        return None

    async def decided(self, view: ApprovalView) -> None:
        if view.id not in self.entered:
            return
        self.entered[view.id].set()
        try:
            await self.release[view.id].wait()
        except asyncio.CancelledError:
            self.cancelled.append(view.id)
            raise
        self.finished.append(view.id)

    async def wait_entered(self, approval_id: str) -> None:
        await asyncio.wait_for(self.entered[approval_id].wait(), timeout=10)


class RepairEnv:
    def __init__(
        self,
        stall: StallEnv,
        *,
        paths: DataPaths,
        keystore: MemoryKeyStore,
        model_http: httpx2.AsyncClient,
        fake: RunningFake,
        definitions: Path,
        write_tool: DummyWriteTool,
    ) -> None:
        self.stall = stall
        self.databases = stall.databases
        self.fake = fake
        self.write_tool = write_tool
        self.sessions = SessionStore(stall.databases)
        self._paths = paths
        self._keystore = keystore
        self._model_http = model_http
        self._definitions = definitions
        store = stall.session.approval_store
        self.first = App(stall.session.manager, store, stall.approvals, observed(store))
        self.apps: list[App] = [self.first]
        self.turns = TurnCounter()  # every restarted app's turn observer

    @property
    def project_id(self) -> str:
        return self.stall.project_id

    def restart(self, manager_cls: type[SessionManager] = SessionManager) -> App:
        """A new app over the same databases: a fresh key failover, approval store, sessions
        and approvals; its observer is the store's first."""
        tools = make_tool_reg(self.write_tool)
        catalog = AgentCatalog.load(tools.names() | STAGE_2_TOOLS, extra_dirs=[self._definitions])
        failover = KeyFailover(FakeClock())
        providers = ProvidersService(self.databases, self._keystore, self._model_http, failover)
        http = httpx2.AsyncClient()
        github = GitHubAuth(self.databases, self._keystore, http, FakeGh(None))
        workspace = WorkspaceService(
            load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(self._paths.root)}),
            self._paths,
            self.databases,
            GitRunner(self._paths.bin, github.token),
            github,
            http,
            memory_project_secrets(self.databases),
        )
        store = ApprovalStore(self.databases)
        observer = observed(store)
        audit_log, event_log = self.stall.audit, self.stall.events
        manager = manager_cls(
            databases=self.databases,
            workspace=workspace,
            catalog=catalog,
            tools=tools,
            settings=AgentSettingsService(self.databases, providers, catalog),
            providers=providers,
            http=self._model_http,
            failover=failover,
            gate=ApprovalGate(
                self.databases, store=store, audit_log=audit_log, event_log=event_log
            ),
            inbound=NoInbound(),
            turn_end=NoTurnEndCheck(),
            workspaces=FakeWorkspaces(workspace=workspace, git=GitRunner(self._paths.bin)),
            approval_store=store,
            observers=[self.turns],
            audit_log=audit_log,
            event_log=event_log,
        )
        service = ApprovalsService(
            self.databases, manager, store=store, audit_log=audit_log, event_log=event_log
        )
        manager.bind_approvals(service)
        app = App(manager, store, service, observer)
        self.apps.append(app)
        return app

    async def ask(self, calls: int = 1) -> tuple[str, list[ApprovalRecord]]:
        """Send to the writer, whose step makes `calls` writes, each asking for approval; the
        turn and its approvals once it waits."""
        self.fake.state.scripted_tool_calls = [
            {"name": "write_code_tool", "arguments": {"file_path": f"f{n}.py", "content": "x"}}
            for n in range(calls)
        ]
        stream = await opened_stream(self.stall.session, WRITER)
        await self.first.manager.send(self.project_id, WRITER, "Please write")
        waited = await collect_until_turn_waiting(stream)
        turn_id = next(e for e in waited if isinstance(e, TurnWaiting)).data.turn_id
        await stream.aclose()
        return turn_id, await self.first.store.list_for_step(self.project_id, turn_id)

    async def ask_stall(self, agent: str = "orchestrator") -> ApprovalRecord:
        """Send to `agent` while the provider's one key is refused: its turn waits on a stall."""
        self.fake.state.script_for_key(KEY, ["401"])
        stream = await opened_stream(self.stall.session, agent)
        await self.first.manager.send(self.project_id, agent, "hi")
        await collect_until_turn_waiting(stream)
        await stream.aclose()
        [stall] = [a for a in await self.first.store.pending(self.project_id) if a.kind == "stall"]
        return stall

    async def seed(self, turn_id: str, *, turn_status: str) -> ApprovalRecord:
        """A pending tool approval and its card, on a new turn of a session of its own."""
        approval_id, session_id, agent = f"apr_{turn_id}", f"ses_{turn_id}", f"agent_{turn_id}"
        card = ApprovalPart(
            type="approval",
            approval_id=approval_id,
            tool_call_id=f"call_{turn_id}",
            preview=TOOL_PREVIEW,
            status="pending",
            note=None,
        )
        message_id = await add_turn(
            self.databases,
            self.project_id,
            session_id=session_id,
            turn_id=turn_id,
            agent=agent,
            status=turn_status,
            parts=[card],
        )
        return await self.first.store.create(
            self.project_id,
            id=approval_id,
            turn_id=turn_id,
            session_id=session_id,
            message_id=message_id,
            agent=agent,
            tool_call_id=card.tool_call_id,
            tool="write_code_tool",
            risk_class=RiskClass.WRITE_CODE,
            preview=TOOL_PREVIEW,
        )

    async def decide(
        self, app: App, approval_id: str, decision: Decision, note: str | None = None
    ) -> None:
        if decision == "cancel":
            await app.service.cancel(self.project_id, approval_id)
        else:
            await app.service.decide(
                self.project_id, approval_id, approve=decision == "approve", note=note
            )

    async def crash_deciding(
        self, approval_id: str, decision: Decision = "approve", note: str | None = None
    ) -> None:
        """Decide on the first app and die right after the status commit."""
        self.first.store.add_observer(CrashOnDecided())
        try:
            await self.decide(self.first, approval_id, decision, note)
        except asyncio.CancelledError:
            return
        raise AssertionError("the decision did not crash")

    async def stream(self, app: App, agent: str = WRITER) -> AsyncGenerator[StreamEvent, None]:
        sub = app.manager.subscribe(self.project_id, agent, after_seq=None)
        await anext(sub)  # the opening resync
        return sub

    async def ended(self, stream: AsyncGenerator[StreamEvent, None]) -> tuple[str, str]:
        found = await collect_until_turn_ended(stream, timeout=10)
        last = next(e for e in reversed(found) if isinstance(e, TurnEnded))
        await self.turns.wait_ended(last.data.turn_id)
        return last.data.turn_id, last.data.status

    async def stored(self, approval_id: str) -> ApprovalRecord:
        record = await self.first.store.get(self.project_id, approval_id)
        assert record is not None
        return record

    async def message_of(self, approval: ApprovalRecord) -> MessageView:
        """The message holding the approval's card."""
        message = await self.sessions.message(
            self.project_id, approval.session_id, approval.message_id
        )
        assert message is not None
        return message

    async def card(self, approval: ApprovalRecord) -> ApprovalPart:
        message = await self.message_of(approval)
        return next(
            p for p in message.parts if isinstance(p, ApprovalPart) and p.approval_id == approval.id
        )

    async def audit_of(self, approval_id: str) -> list[tuple[str, str]]:
        """(actor, decision) of each audit record of the approval."""
        query = select(audit.c.actor, audit.c.decision).where(
            audit.c.action == "approval",
            func.json_extract(audit.c.detail, "$.approval_id") == approval_id,
        )
        engine = await self.databases.project(self.project_id)
        async with engine.connect() as conn:
            return [(str(a), str(d)) for a, d in (await conn.execute(query)).all()]

    async def events_of(self, approval_id: str) -> list[tuple[str, str]]:
        """(source, status) of each approval.decided event of the approval."""
        query = select(events.c.source, func.json_extract(events.c.payload, "$.status")).where(
            events.c.type == "approval.decided", events.c.subject_id == approval_id
        )
        engine = await self.databases.project(self.project_id)
        async with engine.connect() as conn:
            return [(str(s), str(status)) for s, status in (await conn.execute(query)).all()]

    def ends(self, turn_id: str) -> list[str]:
        """How the turn ended on the restarted apps, each time it did."""
        return [status for t, status in self.turns.ended if t == turn_id]

    def waits(self, turn_id: str) -> int:
        """How many times the turn waited again on the restarted apps."""
        return self.turns.waited.count(turn_id)

    async def stall_resumed(self, turn_id: str) -> int:
        """How many times a stall's Try again ran the turn's stalled node again."""
        found = await self.stall.events.history(self.project_id, types=["turn.resumed"])
        return sum(1 for e in found if e.payload == {"turn_id": turn_id})

    async def turn_status(self, turn_id: str) -> str | None:
        return await self.sessions.turn_status(self.project_id, turn_id)

    async def running(self, app: App) -> bool:
        """Whether any worker of the app runs a turn of the project."""
        return "running" in (await app.manager.turn_states(self.project_id)).values()

    async def close(self) -> None:
        for app in reversed(self.apps):
            await app.manager.close()


async def build_repair_env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> RepairEnv:
    write_tool = DummyWriteTool()
    definitions = write_agent_def(tmp_path)
    (definitions / f"{QUIET}.md").write_text(
        f"---\nname: {QUIET}\ndisplay_name: Quiet\ndescription: No chat\nchat: false\n---\nPrompt\n",
        encoding="utf-8",
    )
    stall = await build_stall_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake,
        model_http,
        git_env,
        tools=make_tool_reg(write_tool),
        extra_dirs=[definitions],
    )
    await stall.session.settings.set_default_model(WRITER, f"{stall.session.provider}:fake-tools")
    return RepairEnv(
        stall,
        paths=paths,
        keystore=keystore,
        model_http=model_http,
        fake=fake,
        definitions=definitions,
        write_tool=write_tool,
    )
