"""A cancel of the worker after its turn task ended, or while a Stop unwinds it, keeps that end."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import TurnEnd
from mlops_agents.agents.runtime.executor import TurnExecutor
from mlops_agents.agents.runtime.graph import TurnSink
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.observer_fakes import WorkerCapturingObserver, turn_statuses
from tests.agents.runtime.session_env import (
    agent_message_statuses,
    build_session_env,
    opened_stream,
    turn_ended_statuses,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.stream_helpers import through_first_delta

AGENT = "orchestrator"


async def test_a_cancel_in_the_pass_the_turn_task_returns_keeps_the_turn_done(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = WorkerCapturingObserver()
    run_turn = TurnExecutor.run

    async def run_then_cancel_the_worker(
        self: TurnExecutor,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str,
        sink: TurnSink,
        lock: AbstractAsyncContextManager[Any],
    ) -> tuple[TurnEnd, str | None]:
        result = await run_turn(self, project_id, session_id, agent, turn_id, sink, lock)
        # Queued before the turn task's end wakes the worker: the worker's wait on it raises
        # the cancel although the turn task returned.
        [worker] = observer.workers
        asyncio.get_running_loop().call_soon(worker.cancel)
        return result

    monkeypatch.setattr(TurnExecutor, "run", run_then_cancel_the_worker)
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
    sub = await opened_stream(env)
    await env.manager.send(env.project_id, AGENT, "Check readme")

    assert await asyncio.wait_for(observer.ended.get(), timeout=10) == "done"
    [worker] = observer.workers
    await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), timeout=10)
    assert worker.cancelled()  # the worker's cancel still ends it
    await asyncio.wait_for(env.manager.close(), timeout=10)

    assert await turn_ended_statuses(sub) == ["done"]
    assert await turn_statuses(databases, env.project_id) == ["done"]
    assert await agent_message_statuses(env) == ["complete"]
    assert [(c[0], c[4]) for c in observer.calls if c[0] == "ended"] == [("ended", "done")]
    assert observer.ended.qsize() == 0


async def test_a_cancel_in_the_pass_a_stopped_turn_task_ends_keeps_the_turn_stopped(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = WorkerCapturingObserver()
    run_turn = TurnExecutor.run

    async def run_then_cancel_the_worker_on_stop(
        self: TurnExecutor,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str,
        sink: TurnSink,
        lock: AbstractAsyncContextManager[Any],
    ) -> tuple[TurnEnd, str | None]:
        try:
            return await run_turn(self, project_id, session_id, agent, turn_id, sink, lock)
        except asyncio.CancelledError:
            # Queued before the stopped turn task's end wakes the worker.
            [worker] = observer.workers
            asyncio.get_running_loop().call_soon(worker.cancel)
            raise

    monkeypatch.setattr(TurnExecutor, "run", run_then_cancel_the_worker_on_stop)
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
    await through_first_delta(sub)
    await env.manager.stop(env.project_id, AGENT)

    assert await asyncio.wait_for(observer.ended.get(), timeout=10) == "stopped"
    [worker] = observer.workers
    await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), timeout=10)
    assert worker.cancelled()  # the worker's cancel still ends it
    fake_openai.state.release_slow()
    await asyncio.wait_for(env.manager.close(), timeout=10)

    assert await turn_ended_statuses(sub) == ["stopped"]
    assert await turn_statuses(databases, env.project_id) == ["stopped"]
    assert await agent_message_statuses(env) == ["stopped"]
    assert [(c[0], c[4]) for c in observer.calls if c[0] == "ended"] == [("ended", "stopped")]
    assert observer.ended.qsize() == 0


async def test_a_close_while_a_stopped_turn_task_unwinds_keeps_the_turn_stopped(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unwinding = asyncio.Event()
    run_turn = TurnExecutor.run

    async def run_unwinding_slowly_on_stop(
        self: TurnExecutor,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str,
        sink: TurnSink,
        lock: AbstractAsyncContextManager[Any],
    ) -> tuple[TurnEnd, str | None]:
        try:
            return await run_turn(self, project_id, session_id, agent, turn_id, sink, lock)
        except asyncio.CancelledError:
            # The message is already stopped; the turn task waits until close() cancels it.
            unwinding.set()
            await asyncio.Event().wait()
            raise

    monkeypatch.setattr(TurnExecutor, "run", run_unwinding_slowly_on_stop)
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
    await through_first_delta(sub)
    await env.manager.stop(env.project_id, AGENT)
    await asyncio.wait_for(unwinding.wait(), timeout=10)

    await asyncio.wait_for(env.manager.close(), timeout=10)
    fake_openai.state.release_slow()

    # A Stop was requested and the turn task ended cancelled before the worker woke, so the
    # turn ends stopped, not interrupted.
    assert await turn_ended_statuses(sub) == ["stopped"]
    assert await turn_statuses(databases, env.project_id) == ["stopped"]
    assert await agent_message_statuses(env) == ["stopped"]
    assert [(c[0], c[4]) for c in observer.calls if c[0] == "ended"] == [("ended", "stopped")]
    assert observer.ended.qsize() == 1
