"""Tests for EventTurnObserver (Task 2 of Stage 2 Events)."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from pathlib import Path
from typing import Any

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import ErrorKind, TurnEnd, TurnEnded
from mlops_agents.agents.runtime.executor import TurnExecutor
from mlops_agents.events.contract import EventRecord
from mlops_agents.events.hub import EventHub
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.events.observer import EventTurnObserver
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import build_session_env, collect_until_turn_ended
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

# A turn_ended observer this slow still ends well within the observer timeout; it makes the
# session stream's turn.ended arrive long before the event log holds it (#280).
SLOW_OBSERVER_S = 0.3


class SlowEndObserver:
    """The event log's observer, made slow to hear a turn's end."""

    def __init__(self, inner: EventTurnObserver, delay: float) -> None:
        self._inner = inner
        self._delay = delay

    async def turn_started(self, project_id: str, agent: str, turn_id: str) -> None:
        await self._inner.turn_started(project_id, agent, turn_id)

    async def turn_waiting(self, project_id: str, agent: str, turn_id: str) -> None:
        await self._inner.turn_waiting(project_id, agent, turn_id)

    async def turn_ended(
        self,
        project_id: str,
        agent: str,
        turn_id: str,
        status: TurnEnd,
        final_text: str | None,
        error_kind: ErrorKind | None,
    ) -> None:
        await asyncio.sleep(self._delay)
        await self._inner.turn_ended(project_id, agent, turn_id, status, final_text, error_kind)


async def logged_turn_end(event_log: SqliteEventLog, project_id: str, turn_id: str) -> None:
    """Wait until the event log holds the turn's end. The session stream's turn.ended does not
    wait for the observers; the project stream (replayed from the start, then live) emits only
    what the log has recorded, as the UI reads it (#280)."""

    async def read() -> None:
        async with aclosing(event_log.subscribe(project_id, after_seq=0)) as stream:
            async for event in stream:
                if (
                    isinstance(event, EventRecord)
                    and event.type == "turn.ended"
                    and event.subject_id == turn_id
                ):
                    return

    await asyncio.wait_for(read(), timeout=10)


@pytest.mark.asyncio
async def test_observer_records_lifecycle_and_caps_final_text(databases: Databases) -> None:
    project_id = "prj_obs1"
    hub = EventHub()
    event_log = SqliteEventLog(databases, hub=hub)
    observer = EventTurnObserver(event_log)

    turn_id = "trn_test1"
    agent = "orchestrator"

    # 1. turn_started
    await observer.turn_started(project_id, agent, turn_id)
    history = await event_log.history(project_id, types=["turn.started"])
    assert len(history) == 1
    assert history[0].source == agent
    assert history[0].type == "turn.started"
    assert history[0].subject_id == turn_id
    assert history[0].payload == {"turn_id": turn_id}

    # 2. turn_waiting
    await observer.turn_waiting(project_id, agent, turn_id)
    history = await event_log.history(project_id, types=["turn.waiting"])
    assert len(history) == 1
    assert history[0].source == agent
    assert history[0].type == "turn.waiting"
    assert history[0].subject_id == turn_id
    assert history[0].payload == {"turn_id": turn_id}

    # 3. turn_ended with final_text > 200 chars
    long_text = "A" * 350
    await observer.turn_ended(project_id, agent, turn_id, "done", long_text, None)
    history = await event_log.history(project_id, types=["turn.ended"])
    assert len(history) == 1
    assert history[0].source == agent
    assert history[0].type == "turn.ended"
    assert history[0].subject_id == turn_id
    payload = history[0].payload
    assert payload["turn_id"] == turn_id
    assert payload["status"] == "done"
    assert len(payload["final_text"]) == 200
    assert payload["final_text"] == "A" * 200
    assert payload["error_kind"] is None

    # 4. turn_ended with None final_text, failed for want of a usable key
    turn_id_2 = "trn_test2"
    await observer.turn_ended(project_id, agent, turn_id_2, "failed", None, "keys_exhausted")
    history = await event_log.history(project_id, types=["turn.ended"], limit=10)
    assert len(history) == 2
    # Newest first
    assert history[0].subject_id == turn_id_2
    assert history[0].payload == {
        "turn_id": turn_id_2,
        "status": "failed",
        "final_text": None,
        "error_kind": "keys_exhausted",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("observer_delay", [0.0, SLOW_OBSERVER_S])
async def test_turn_on_fake_model_records_turn_events(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    observer_delay: float,
) -> None:
    event_log = SqliteEventLog(databases)
    observer = SlowEndObserver(EventTurnObserver(event_log), observer_delay)

    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        observers=[observer],
    )
    manager = env.manager
    project_id = env.project_id
    agent = "orchestrator"

    view = await manager.get(project_id, agent)
    sub = manager.subscribe(project_id, agent, after_seq=view.last_seq)

    await manager.send(project_id, agent, "Check readme")
    events = await collect_until_turn_ended(sub)
    ended = events[-1]
    assert isinstance(ended, TurnEnded)
    turn_id = ended.data.turn_id

    await logged_turn_end(event_log, project_id, turn_id)
    history = await event_log.history(project_id, types=["turn.started", "turn.ended"], limit=10)
    # history is newest first: [turn.ended, turn.started]
    assert len(history) == 2
    assert history[0].type == "turn.ended"
    assert history[0].source == agent
    assert history[0].subject_id == turn_id
    assert history[0].payload["turn_id"] == turn_id
    assert history[0].payload["status"] == "done"
    assert isinstance(history[0].payload["final_text"], str)
    assert len(history[0].payload["final_text"]) <= 200

    assert history[1].type == "turn.started"
    assert history[1].source == agent
    assert history[1].subject_id == turn_id
    assert history[1].payload == {"turn_id": turn_id}

    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("observer_delay", [0.0, SLOW_OBSERVER_S])
async def test_failed_turn_records_failed_turn_ended(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    observer_delay: float,
) -> None:
    event_log = SqliteEventLog(databases)
    observer = SlowEndObserver(EventTurnObserver(event_log), observer_delay)

    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        observers=[observer],
    )
    manager = env.manager
    project_id = env.project_id
    agent = "orchestrator"

    async def fail_turn(self: TurnExecutor, *args: Any, **kwargs: Any) -> tuple[str, str]:
        session_id, turn_id = args[1], args[3]
        await env.store.add_message(
            project_id,
            session_id,
            turn_id=turn_id,
            author_kind="agent",
            author_name=agent,
            parts=[],
            status="streaming",
        )
        raise RuntimeError("simulated executor crash")

    monkeypatch.setattr(TurnExecutor, "run", fail_turn)

    view = await manager.get(project_id, agent)
    sub = manager.subscribe(project_id, agent, after_seq=view.last_seq)

    await manager.send(project_id, agent, "Will crash")
    events = await collect_until_turn_ended(sub)
    ended = events[-1]
    assert isinstance(ended, TurnEnded)
    assert ended.data.status == "failed"
    turn_id = ended.data.turn_id

    await logged_turn_end(event_log, project_id, turn_id)
    history = await event_log.history(project_id, types=["turn.started", "turn.ended"], limit=10)
    assert len(history) == 2
    assert history[0].type == "turn.ended"
    assert history[0].source == agent
    assert history[0].subject_id == turn_id
    assert history[0].payload == {
        "turn_id": turn_id,
        "status": "failed",
        "final_text": None,
        "error_kind": None,
    }

    assert history[1].type == "turn.started"
    assert history[1].source == agent
    assert history[1].subject_id == turn_id
    assert history[1].payload == {"turn_id": turn_id}

    await manager.close()
