"""A cancel of the worker that lands after a turn's end was decided keeps that end."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import TurnEnd
from mlops_agents.agents.runtime.executor import TurnExecutor
from mlops_agents.agents.runtime.graph import TurnSink
from mlops_agents.agents.runtime.shielded import shielded
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.observer_fakes import WorkerCapturingObserver, turn_statuses
from tests.agents.runtime.session_env import (
    SessionEnv,
    agent_message_statuses,
    build_session_env,
    opened_stream,
    turn_ended_statuses,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.stream_helpers import through_first_delta

AGENT = "orchestrator"


@dataclass
class HeldEndWrite:
    """Holds the turn's write of one terminal status, as a shielded write a cancel lands in."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


def hold_end_write(monkeypatch: pytest.MonkeyPatch, status: str) -> HeldEndWrite:
    held = HeldEndWrite()
    set_turn = SessionStore.set_turn

    @shielded
    async def held_write(
        self: SessionStore, project_id: str, turn_id: str, *, error: str | None
    ) -> None:
        held.entered.set()
        await held.release.wait()
        await set_turn(self, project_id, turn_id, status, error=error)

    async def holding_set_turn(
        self: SessionStore, project_id: str, turn_id: str, to: str, *, error: str | None = None
    ) -> None:
        if to != status:
            await set_turn(self, project_id, turn_id, to, error=error)
            return
        await held_write(self, project_id, turn_id, error=error)

    monkeypatch.setattr(SessionStore, "set_turn", holding_set_turn)
    return held


async def close_during(
    env: SessionEnv, held: HeldEndWrite, observer: WorkerCapturingObserver
) -> None:
    """Close the manager while the end write is held, then let the write finish."""
    await asyncio.wait_for(held.entered.wait(), timeout=10)
    [worker] = observer.workers
    closing = asyncio.create_task(env.manager.close())
    # One pass of the loop, not a delay: close() cancels the workers before its first await.
    await asyncio.sleep(0)
    assert worker.cancelling() > 0
    held.release.set()
    await asyncio.wait_for(closing, timeout=10)


async def test_a_cancel_during_the_done_write_keeps_the_turn_done(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = hold_end_write(monkeypatch, "done")
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
    sub = await opened_stream(env)
    await env.manager.send(env.project_id, AGENT, "Check readme")

    await close_during(env, held, observer)

    assert await turn_ended_statuses(sub) == ["done"]
    assert await turn_statuses(databases, env.project_id) == ["done"]
    assert await agent_message_statuses(env) == ["complete"]
    assert [(c[0], c[4]) for c in observer.calls if c[0] == "ended"] == [("ended", "done")]
    assert observer.ended.qsize() == 1


async def test_a_cancel_during_the_stopped_write_keeps_the_turn_stopped(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = hold_end_write(monkeypatch, "stopped")
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

    await close_during(env, held, observer)
    fake_openai.state.release_slow()

    assert await turn_ended_statuses(sub) == ["stopped"]
    assert await turn_statuses(databases, env.project_id) == ["stopped"]
    # The user's Stop shows as such, never as an interruption.
    assert await agent_message_statuses(env) == ["stopped"]
    assert [(c[0], c[4]) for c in observer.calls if c[0] == "ended"] == [("ended", "stopped")]
    assert observer.ended.qsize() == 1


async def test_a_cancel_during_the_failed_write_keeps_the_turn_failed(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_run(
        self: TurnExecutor,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str,
        sink: TurnSink,
        lock: AbstractAsyncContextManager[Any],
    ) -> tuple[TurnEnd, str | None]:
        raise RuntimeError("an error that escapes the executor")

    monkeypatch.setattr(TurnExecutor, "run", failing_run)
    held = hold_end_write(monkeypatch, "failed")
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
    sub = await opened_stream(env)
    await env.manager.send(env.project_id, AGENT, "Check readme")

    await close_during(env, held, observer)

    assert await turn_ended_statuses(sub) == ["failed"]
    assert await turn_statuses(databases, env.project_id) == ["failed"]
    session_id = await env.store.session_id(env.project_id, AGENT)
    authors = [m.author_kind for m in await env.store.messages(env.project_id, session_id)]
    assert authors == ["user", "system"]  # the failure notice
    assert [(c[0], c[4]) for c in observer.calls if c[0] == "ended"] == [("ended", "failed")]
    assert observer.ended.qsize() == 1
