"""A cancel of the turn task by neither Stop nor a close ends that turn, and the queue goes on."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import TurnEnded
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.observer_fakes import WorkerCapturingObserver, turn_statuses
from tests.agents.runtime.session_env import (
    agent_message_statuses,
    build_session_env,
    collect_until_turn_ended,
    opened_stream,
)
from tests.agents.runtime.turn_tasks import record_turn_tasks
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.stream_helpers import through_first_delta

AGENT = "orchestrator"


async def test_an_outside_cancel_ends_the_turn_interrupted_and_the_next_turn_runs(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn_tasks = record_turn_tasks(monkeypatch)
    observer = WorkerCapturingObserver()
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
    await env.settings.set_default_model(AGENT, f"{env.provider}:fake-slow")
    fake_openai.state.hold_slow()
    sub = await opened_stream(env)
    await env.manager.send(env.project_id, AGENT, "Slow response")
    queued = await env.manager.send(env.project_id, AGENT, "Slow response again")
    assert queued.status == "queued"
    await through_first_delta(sub)

    [turn_task] = turn_tasks
    turn_task.cancel()
    first = (await collect_until_turn_ended(sub, timeout=10))[-1]
    fake_openai.state.release_slow()
    second = (await collect_until_turn_ended(sub, timeout=10))[-1]

    assert isinstance(first, TurnEnded) and isinstance(second, TurnEnded)
    assert first.data.turn_id != second.data.turn_id
    assert (first.data.status, second.data.status) == ("interrupted", "done")
    for _ in range(2):
        await asyncio.wait_for(observer.ended.get(), timeout=10)
    assert [(c[0], c[4] if c[0] == "ended" else None) for c in observer.calls] == [
        ("started", None),
        ("ended", "interrupted"),
        ("started", None),
        ("ended", "done"),
    ]
    # One worker took both turns and ended once the queue was empty, not by the cancel.
    first_worker, second_worker = observer.workers
    assert first_worker is second_worker
    await asyncio.wait_for(asyncio.gather(first_worker, return_exceptions=True), timeout=10)
    assert not first_worker.cancelled() and first_worker.exception() is None
    assert sorted(await turn_statuses(databases, env.project_id)) == ["done", "interrupted"]
    assert await agent_message_statuses(env) == ["interrupted", "complete"]

    await env.manager.close()
