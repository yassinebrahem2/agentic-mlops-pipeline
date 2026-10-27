"""`waiting` has one writer, the executor's wait (`handle_turn_waiting`), and a session with a
waiting turn is busy: no queued turn runs before that turn is resumed and ends (#276, Decision
#281)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path

import httpx2
import pytest
from langgraph.types import interrupt

from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.service import ApprovalsService
from mlops_agents.agents.runtime.contract import StreamEvent, TurnEnded, TurnWaiting
from mlops_agents.agents.runtime.hooks import GateCall, GateDecision
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.hook_fakes import ALLOW
from tests.agents.runtime.observer_fakes import RecordingObserver
from tests.agents.runtime.session_env import build_session_env, opened_stream
from tests.agents.runtime.test_resuming_turns import make_tool_reg, write_agent_def
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


class AskFirstCallGate:
    """Asks for approval of the first call it sees and allows every later one."""

    def __init__(self) -> None:
        self.asked = False

    async def decide(self, calls: Sequence[GateCall]) -> dict[str, GateDecision]:
        decisions: dict[str, GateDecision] = {}
        for call in calls:
            if self.asked:
                decisions[call.tool_call_id] = ALLOW
                continue
            self.asked = True
            preview = await call.tool.preview(call.ctx, call.args)
            decisions[call.tool_call_id] = GateDecision(
                outcome="ask", message=None, approval_id="apr_first", preview=preview
            )
        return decisions

    async def wait(
        self, project_id: str, turn_id: str, decisions: dict[str, GateDecision]
    ) -> dict[str, GateDecision]:
        interrupt(None)
        return decisions


class WaitRecordingObserver(RecordingObserver):
    """Also queues each wait it hears, so a test can wait for the observers themselves."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: asyncio.Queue[str] = asyncio.Queue()

    async def turn_waiting(self, project_id: str, agent: str, turn_id: str) -> None:
        await super().turn_waiting(project_id, agent, turn_id)
        self.waits.put_nowait(turn_id)


async def _until(
    sub: AsyncGenerator[StreamEvent, None], events: list[StreamEvent], count: int, kind: type
) -> None:
    """Read the stream into `events` until it holds `count` events of `kind`."""

    async def read() -> None:
        while sum(isinstance(e, kind) for e in events) < count:
            events.append(await anext(sub))

    await asyncio.wait_for(read(), timeout=10)


async def test_a_wait_that_cannot_be_stored_fails_the_turn_and_the_next_turn_runs(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    set_turn = SessionStore.set_turn
    waiting_writes: list[str] = []

    async def failing_set_turn(
        self: SessionStore, project_id: str, turn_id: str, status: str, *, error: str | None = None
    ) -> None:
        if status == "waiting":
            waiting_writes.append(turn_id)
            raise RuntimeError("disk full writing waiting")
        await set_turn(self, project_id, turn_id, status, error=error)

    monkeypatch.setattr(SessionStore, "set_turn", failing_set_turn)
    observer = RecordingObserver()
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        observers=[observer],
        gate=AskFirstCallGate(),
    )
    sub = await opened_stream(env)
    await env.manager.send(env.project_id, "orchestrator", "Check readme")
    queued = await env.manager.send(env.project_id, "orchestrator", "Check readme again")
    assert queued.status == "queued"

    events: list[StreamEvent] = []
    await _until(sub, events, 2, TurnEnded)
    for _ in range(2):
        await asyncio.wait_for(observer.ended.get(), timeout=10)

    # The executor's write is the one write of the wait; it failed, so the turn failed.
    [first_turn] = waiting_writes
    assert [e for e in events if isinstance(e, TurnWaiting)] == []
    ends = [(e.data.turn_id, e.data.status) for e in events if isinstance(e, TurnEnded)]
    [second_turn] = [turn for turn, _ in ends if turn != first_turn]
    assert ends == [(first_turn, "failed"), (second_turn, "done")]
    assert [(c[0], c[3], c[4] if c[0] == "ended" else None) for c in observer.calls] == [
        ("started", first_turn, None),
        ("ended", first_turn, "failed"),
        ("started", second_turn, None),
        ("ended", second_turn, "done"),
    ]
    assert f"Turn {first_turn} failed unexpectedly" in caplog.text
    assert "never retrieved" not in caplog.text
    await env.manager.close()


async def test_a_queued_turn_waits_until_the_waiting_turn_is_resumed_and_ends(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A second write of the same wait, if anything made one, fails: it must not let the
    # queue run while the stored wait stands.
    set_turn = SessionStore.set_turn
    waiting_writes: list[str] = []

    async def second_wait_write_fails(
        self: SessionStore, project_id: str, turn_id: str, status: str, *, error: str | None = None
    ) -> None:
        if status == "waiting":
            waiting_writes.append(turn_id)
            if waiting_writes.count(turn_id) > 1:
                raise RuntimeError("disk full writing waiting again")
        await set_turn(self, project_id, turn_id, status, error=error)

    monkeypatch.setattr(SessionStore, "set_turn", second_wait_write_fails)
    observer = WaitRecordingObserver()
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        extra_dirs=[write_agent_def(tmp_path)],
        tools=make_tool_reg(),
        observers=[observer],
        gate=ApprovalGate(databases),
    )
    await env.settings.set_default_model("writer", f"{env.provider}:fake-tools")
    fake_openai.state.scripted_tool_calls = [
        {"name": "write_code_tool", "arguments": {"file_path": "a.py", "content": "print(1)"}}
    ]
    approvals = ApprovalsService(databases, env.manager)
    sub = await opened_stream(env, "writer")
    await env.manager.send(env.project_id, "writer", "Write a.py")
    queued = await env.manager.send(env.project_id, "writer", "Write b.py")
    assert queued.status == "queued"

    events: list[StreamEvent] = []
    await _until(sub, events, 1, TurnWaiting)
    [first_wait] = [e for e in events if isinstance(e, TurnWaiting)]
    first_turn = first_wait.data.turn_id
    [approval] = [a for a in await approvals.pending(env.project_id) if a.turn_id == first_turn]

    await approvals.decide(env.project_id, approval.id, approve=True)
    await _until(sub, events, 2, TurnWaiting)

    # The observers hear a wait after the stream does: wait for them, not the stream.
    for _ in range(2):
        await asyncio.wait_for(observer.waits.get(), timeout=10)
    assert waiting_writes.count(first_turn) == 1
    [second_turn] = [c[3] for c in observer.calls if c[0] == "started" and c[3] != first_turn]
    assert [(c[0], c[3]) for c in observer.calls] == [
        ("started", first_turn),
        ("waiting", first_turn),
        ("ended", first_turn),
        ("started", second_turn),
        ("waiting", second_turn),
    ]
    await env.manager.close()
