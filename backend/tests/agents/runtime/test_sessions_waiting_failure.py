"""A turn whose wait for approval cannot be stored is still told waiting, once, and the worker
goes on to the next queued turn (#276, Decision #249)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

import httpx2
import pytest
from langgraph.types import interrupt

from mlops_agents.agents.runtime.contract import StreamEvent, TurnEnded, TurnWaiting
from mlops_agents.agents.runtime.hooks import GateCall, GateDecision
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.hook_fakes import ALLOW
from tests.agents.runtime.observer_fakes import RecordingObserver
from tests.agents.runtime.session_env import build_session_env, opened_stream
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

AGENT = "orchestrator"


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


async def test_a_wait_that_cannot_be_stored_is_told_once_and_the_next_turn_runs(
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
    await env.manager.send(env.project_id, AGENT, "Check readme")
    queued = await env.manager.send(env.project_id, AGENT, "Check readme again")
    assert queued.status == "queued"

    events: list[StreamEvent] = []

    async def until_a_turn_ends() -> None:
        async for event in sub:
            events.append(event)
            if isinstance(event, TurnEnded):
                return

    await asyncio.wait_for(until_a_turn_ends(), timeout=10)
    await asyncio.wait_for(observer.ended.get(), timeout=10)

    [first_turn] = set(waiting_writes)
    waits = [e.data.turn_id for e in events if isinstance(e, TurnWaiting)]
    ends = [(e.data.turn_id, e.data.status) for e in events if isinstance(e, TurnEnded)]
    assert waits == [first_turn]
    [(second_turn, second_status)] = ends
    assert (second_turn != first_turn, second_status) == (True, "done")
    assert [(c[0], c[3]) for c in observer.calls] == [
        ("started", first_turn),
        ("waiting", first_turn),
        ("started", second_turn),
        ("ended", second_turn),
    ]
    assert f"Could not record turn {first_turn} waiting" in caplog.text
    assert "never retrieved" not in caplog.text
    await env.manager.close()
