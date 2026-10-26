"""Turn observers when a cancel lands in the end report, or the turn task is cancelled."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import httpx2
import pytest

from mlops_agents.agents.runtime.executor import TurnExecutor, TurnOutcome
from mlops_agents.agents.runtime.graph import TurnSink
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.observer_fakes import (
    HeldEndObserver,
    RecordingObserver,
    turn_statuses,
)
from tests.agents.runtime.session_env import build_session_env
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.stream_helpers import through_first_delta

AGENT = "orchestrator"


async def test_a_cancel_during_the_end_report_still_reaches_every_observer(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    held = HeldEndObserver()
    after = RecordingObserver()
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        observers=[held, after],
    )
    manager = env.manager
    await manager.send(env.project_id, AGENT, "Check readme")
    queued = await manager.send(env.project_id, AGENT, "Check readme again")
    assert queued.status == "queued"
    await asyncio.wait_for(held.entered.wait(), timeout=10)

    closing = asyncio.create_task(manager.close())
    # One pass of the loop, not a delay: close() cancels the workers before its first await.
    await asyncio.sleep(0)
    held.release.set()
    await asyncio.wait_for(closing, timeout=10)

    # The observer after the held one still hears the first turn end...
    assert [call[0] for call in after.calls] == ["started", "ended"]
    assert after.calls[1][4] == "done"
    # ...and the cancel still stops the worker: the queued turn never starts.
    assert [call[0] for call in held.calls] == ["started", "ended"]
    assert await turn_statuses(databases, env.project_id) == ["done", "queued"]


async def test_an_outside_cancel_of_the_turn_task_reports_it_interrupted_once(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn_tasks: list[asyncio.Task[Any]] = []
    run_turn = TurnExecutor.run

    async def recording_run(
        self: TurnExecutor,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str,
        sink: TurnSink,
        lock: AbstractAsyncContextManager[Any],
    ) -> TurnOutcome:
        task = asyncio.current_task()
        assert task is not None
        turn_tasks.append(task)
        return await run_turn(self, project_id, session_id, agent, turn_id, sink, lock)

    monkeypatch.setattr(TurnExecutor, "run", recording_run)
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
    )
    manager = env.manager
    await env.settings.set_default_model(AGENT, f"{env.provider}:fake-slow")
    fake_openai.state.hold_slow()
    sub = manager.subscribe(env.project_id, AGENT, after_seq=None)
    await anext(sub)  # the opening resync
    await manager.send(env.project_id, AGENT, "Slow response")
    await through_first_delta(sub)

    # Neither Stop nor a close: something else cancels the turn task while the model streams.
    [turn_task] = turn_tasks
    turn_task.cancel()
    assert await asyncio.wait_for(observer.ended.get(), timeout=10) == "interrupted"
    fake_openai.state.release_slow()
    await asyncio.wait_for(manager.close(), timeout=10)

    assert [call[0] for call in observer.calls] == ["started", "ended"]
    assert observer.ended.qsize() == 0
    assert await turn_statuses(databases, env.project_id) == ["interrupted"]
