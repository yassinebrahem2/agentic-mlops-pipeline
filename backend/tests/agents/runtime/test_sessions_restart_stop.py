"""A queued turn left by a restart is started by the next `get`, and not by a Send, so a Stop
that comes before its worker takes that turn is between two turns, and dropped (#574)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import TextPart
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.messaging.test_inbound_stops import WorkerStarts
from tests.agents.repair_doubles import TurnCounter
from tests.agents.runtime.session_env import build_session_env
from tests.agents.turn_end_doubles import hold
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

AGENT = "orchestrator"


async def test_a_stop_before_a_queued_turn_left_by_a_restart_is_taken_leaves_it_running(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = TurnCounter()
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, observers=[turns]
    )
    project_id, manager, store = env.project_id, env.manager, env.store

    # The queued turn a previous process left: this manager has started no worker for it.
    session_id = await store.session_id(project_id, AGENT)
    trigger = await store.add_message(
        project_id,
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Queued before the restart.")],
        status="queued",
    )
    turn_id = await store.add_turn(project_id, session_id, trigger.id, "queued")

    starts = WorkerStarts(monkeypatch)
    first_look = hold(
        monkeypatch, SessionStore, "next_queued_turn", lambda *_a, **_k: starts.is_worker()
    )
    try:
        async with asyncio.timeout(20):
            view = await manager.get(project_id, AGENT)
            assert view.busy
            await first_look.wait_entered()
            requests = len(fake_openai.state.requests)
            await manager.stop(project_id, AGENT)
            first_look.release.set()
            await turns.wait_ended(turn_id)
    finally:
        first_look.release.set()
        await manager.close()

    assert len(starts.workers) == 1
    assert [status for t, status in turns.ended if t == turn_id] == ["done"]
    assert len(fake_openai.state.requests) > requests
