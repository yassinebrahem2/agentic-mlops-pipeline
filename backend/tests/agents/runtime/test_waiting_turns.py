from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from dataclasses import replace
from pathlib import Path

import httpx2
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import interrupt
from sqlalchemy import RowMapping, select

from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.contract import (
    MessageUpdated,
    StreamEvent,
    TurnEnd,
    TurnWaiting,
)
from mlops_agents.agents.runtime.hooks import (
    Gate,
    GateCall,
    GateDecision,
)
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools import default_registry
from mlops_agents.config import RunMode, Settings, load_settings
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.sqlite import connect_checkpointer
from mlops_agents.storage.tables import turns
from mlops_agents.workspace.service import WorkspaceService
from tests.agents.runtime.hook_fakes import NoInbound
from tests.agents.runtime.session_env import build_session_env
from tests.conftest import STAGE_2_TOOLS, MemoryKeyStore, memory_project_secrets
from tests.fakes.fake_openai import RunningFake
from tests.fakes.turn_end import NoTurnEndCheck
from tests.fakes.workspaces import FakeWorkspaces
from tests.integrations.test_github_auth import FakeGh


def _get_settings(paths: DataPaths) -> Settings:
    base = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)})
    return replace(base, mode=RunMode.LOCAL)


class AskGate(Gate):
    """A gate that always asks for approval on any tool call, triggering interrupt."""

    def __init__(self) -> None:
        self.decide_calls: list[GateCall] = []

    async def decide(self, calls: Sequence[GateCall]) -> dict[str, GateDecision]:
        self.decide_calls.extend(calls)
        results: dict[str, GateDecision] = {}
        for call in calls:
            preview = await call.tool.preview(call.ctx, call.args)
            apr_id = f"apr_{call.tool_call_id}"
            results[call.tool_call_id] = GateDecision(
                outcome="ask",
                message=None,
                approval_id=apr_id,
                preview=preview,
            )
        return results

    async def wait(
        self, project_id: str, turn_id: str, decisions: dict[str, GateDecision]
    ) -> dict[str, GateDecision]:
        interrupt(None)
        return decisions


class RecordingObserver:
    def __init__(self) -> None:
        self.started: list[tuple[str, str, str]] = []
        self.waiting: list[tuple[str, str, str]] = []
        self.ended: list[tuple[str, str, str, TurnEnd, str | None]] = []

    async def turn_started(self, project_id: str, agent: str, turn_id: str) -> None:
        self.started.append((project_id, agent, turn_id))

    async def turn_waiting(self, project_id: str, agent: str, turn_id: str) -> None:
        self.waiting.append((project_id, agent, turn_id))

    async def turn_ended(
        self,
        project_id: str,
        agent: str,
        turn_id: str,
        status: TurnEnd,
        final_text: str | None,
    ) -> None:
        self.ended.append((project_id, agent, turn_id, status, final_text))


def _make_manager(
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    model_http: httpx2.AsyncClient,
    gate: Gate,
) -> SessionManager:
    tool_reg = default_registry()
    catalog = AgentCatalog.load(tool_reg.names() | STAGE_2_TOOLS)
    providers_svc = ProvidersService(databases, keystore, model_http)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    http = httpx2.AsyncClient()
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    workspace_svc = WorkspaceService(
        _get_settings(paths),
        paths,
        databases,
        GitRunner(paths.bin, github.token),
        github,
        http,
        memory_project_secrets(databases),
    )
    return SessionManager(
        databases=databases,
        workspace=workspace_svc,
        catalog=catalog,
        tools=tool_reg,
        settings=settings_svc,
        providers=providers_svc,
        http=model_http,
        gate=gate,
        inbound=NoInbound(),
        turn_end=NoTurnEndCheck(),
        workspaces=FakeWorkspaces(workspace=workspace_svc, git=GitRunner(paths.bin)),
        approval_store=ApprovalStore(databases),
    )


async def _get_turn(databases: Databases, project_id: str, turn_id: str) -> RowMapping | None:
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        return (await conn.execute(select(turns).where(turns.c.id == turn_id))).mappings().first()


async def _assert_thread_preserved(db_path: Path, thread_id: str) -> None:
    conn = await connect_checkpointer(db_path)
    saver = AsyncSqliteSaver(conn)
    chk = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    assert chk is not None, f"Expected checkpointer thread {thread_id} to be preserved"
    await conn.close()


async def _assert_connection_closed(db_path: Path) -> None:
    conn = await connect_checkpointer(db_path)
    await conn.close()


async def collect_until_turn_waiting(
    sub: AsyncGenerator[StreamEvent, None],
    timeout: float = 5.0,  # noqa: ASYNC109
) -> list[StreamEvent]:
    events: list[StreamEvent] = []

    async def collector() -> None:
        async for ev in sub:
            events.append(ev)
            if isinstance(ev, TurnWaiting):
                break

    await asyncio.wait_for(collector(), timeout=timeout)
    return events


async def test_gate_ask_triggers_waiting_turn(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    """When a gate asks for approval on a tool call:
    - Turn execution interrupts
    - Turn status in DB becomes 'waiting'
    - Agent message status becomes 'waiting'
    - TurnWaiting event published on sink
    - Observer's turn_waiting called
    - Checkpoint thread preserved
    - Database connection closed (file can be opened/read)
    - No turn.ended event published
    - No turn_ended observer called
    """
    gate = AskGate()
    observer = RecordingObserver()

    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        gate=gate,
        observers=[observer],
    )
    manager = env.manager
    project_id = env.project_id
    store = env.store

    view = await manager.get(project_id, "orchestrator")
    session_id = view.id
    sub = manager.subscribe(project_id, "orchestrator", after_seq=view.last_seq)

    # Sending a message that fake-tools will respond with a tool call
    user_msg = await manager.send(project_id, "orchestrator", "Check readme")
    assert user_msg.status == "complete"

    live_events = await collect_until_turn_waiting(sub)

    # 1. TurnWaiting event was published
    turn_waiting_events = [e for e in live_events if isinstance(e, TurnWaiting)]
    assert len(turn_waiting_events) == 1
    turn_id = turn_waiting_events[0].data.turn_id

    # No turn.ended event should be published
    ended_events = [e for e in live_events if e.type == "turn.ended"]
    assert len(ended_events) == 0

    # 2. Observers called: turn_started called, turn_waiting called, but NOT turn_ended
    assert (project_id, "orchestrator", turn_id) in observer.started
    assert (project_id, "orchestrator", turn_id) in observer.waiting
    assert len(observer.ended) == 0

    # 3. Check turn status in DB: must be 'waiting'
    turn = await _get_turn(databases, project_id, turn_id)
    assert turn is not None
    assert turn["status"] == "waiting"
    assert turn["ended_at"] is None

    # 4. Check agent message in DB: must be 'waiting'
    agent_msg = await store.turn_agent_message(project_id, session_id, turn_id)
    assert agent_msg is not None
    assert agent_msg.status == "waiting"

    # Also verify ApprovalPart is present in message parts
    approval_parts = [p for p in agent_msg.parts if p.type == "approval"]
    assert len(approval_parts) == 1
    assert approval_parts[0].status == "pending"
    assert approval_parts[0].approval_id == f"apr_{approval_parts[0].tool_call_id}"

    # MessageUpdated event with status='waiting' was emitted
    msg_updated_events = [
        e
        for e in live_events
        if isinstance(e, MessageUpdated) and e.data.message.id == agent_msg.id
    ]
    assert len(msg_updated_events) > 0
    assert msg_updated_events[-1].data.message.status == "waiting"

    # 5. Check checkpointer thread is preserved
    db_path = store.db_path(project_id)
    await _assert_thread_preserved(db_path, turn_id)

    # 6. Check sqlite connection is closed (can be connected/closed without lock)
    await _assert_connection_closed(db_path)

    # Clean up
    await manager.close()


async def test_subsequent_messages_queue_behind_waiting_turn(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    """When a turn is waiting, subsequent messages queue and do not execute."""
    gate = AskGate()
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        gate=gate,
    )
    manager = env.manager
    project_id = env.project_id
    store = env.store

    view = await manager.get(project_id, "orchestrator")
    session_id = view.id
    sub = manager.subscribe(project_id, "orchestrator", after_seq=view.last_seq)

    # 1. Trigger the waiting turn
    await manager.send(project_id, "orchestrator", "Check readme")
    await collect_until_turn_waiting(sub)

    # Verify session is busy
    session_view = await manager.get(project_id, "orchestrator")
    assert session_view.busy is True

    # 2. Send second message while turn is waiting
    second_msg = await manager.send(project_id, "orchestrator", "Second message")
    assert second_msg.status == "queued"

    # Allow any asynchronous task to potentially run
    await asyncio.sleep(0.3)

    # 3. Verify second message and its turn remain queued
    stored_second_msg = await store.message(project_id, session_id, second_msg.id)
    assert stored_second_msg is not None
    assert stored_second_msg.status == "queued"

    queued_turn_info = await store.next_queued_turn(project_id, session_id)
    assert queued_turn_info is not None
    queued_turn_id, trigger_id = queued_turn_info
    assert trigger_id == second_msg.id

    queued_turn = await _get_turn(databases, project_id, queued_turn_id)
    assert queued_turn is not None
    assert queued_turn["status"] == "queued"

    await manager.close()


async def test_recovery_preserves_waiting_turn_and_message(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    """Rebuilding / restarting SessionManager leaves waiting turns and waiting messages intact."""
    gate = AskGate()
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        gate=gate,
    )
    manager = env.manager
    project_id = env.project_id
    store = env.store

    view = await manager.get(project_id, "orchestrator")
    session_id = view.id
    sub = manager.subscribe(project_id, "orchestrator", after_seq=view.last_seq)

    # Trigger waiting turn
    await manager.send(project_id, "orchestrator", "Check readme")
    events = await collect_until_turn_waiting(sub)
    turn_waiting_ev = next(e for e in events if isinstance(e, TurnWaiting))
    turn_id = turn_waiting_ev.data.turn_id

    # Close manager (shutdown)
    await manager.close()

    # Re-run recovery via a new store recovery call or rebuilding SessionManager
    await store.recover(project_id, approval_store=ApprovalStore(databases))

    # Verify turn is still waiting
    turn = await _get_turn(databases, project_id, turn_id)
    assert turn is not None
    assert turn["status"] == "waiting"

    # Verify agent message is still waiting
    agent_msg = await store.turn_agent_message(project_id, session_id, turn_id)
    assert agent_msg is not None
    assert agent_msg.status == "waiting"

    # Rebuild manager stack (restart) and verify get() shows busy
    new_manager = _make_manager(paths, databases, keystore, model_http, gate)
    new_view = await new_manager.get(project_id, "orchestrator")
    assert new_view.busy is True

    # Check turn still waiting
    turn_after_get = await _get_turn(databases, project_id, turn_id)
    assert turn_after_get is not None
    assert turn_after_get["status"] == "waiting"

    await new_manager.close()
