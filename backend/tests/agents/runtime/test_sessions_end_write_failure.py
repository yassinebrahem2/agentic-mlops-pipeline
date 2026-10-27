"""A turn whose end cannot be stored keeps its end, is reported ended, and the queue goes on."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import TextPart, TurnEnd, TurnEnded
from mlops_agents.agents.runtime.executor import UNEXPECTED_FAILURE_TEXT, TurnExecutor
from mlops_agents.agents.runtime.graph import TurnSink
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.observer_fakes import (
    HeldObserver,
    RecordingObserver,
    WorkerCapturingObserver,
    turn_statuses,
)
from tests.agents.runtime.session_env import build_session_env, collect_until_turn_ended
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

AGENT = "orchestrator"


async def test_a_failing_end_write_keeps_the_end_and_the_next_turn_runs(
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
    failures = ["disk full"]

    async def set_turn_failing_once(
        self: SessionStore, project_id: str, turn_id: str, status: str, *, error: str | None = None
    ) -> None:
        if status == "done" and failures:
            raise RuntimeError(failures.pop())
        await set_turn(self, project_id, turn_id, status, error=error)

    monkeypatch.setattr(SessionStore, "set_turn", set_turn_failing_once)
    observer = HeldObserver()
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
    sub = manager.subscribe(env.project_id, AGENT, after_seq=None)
    await anext(sub)  # the opening resync
    await manager.send(env.project_id, AGENT, "Check readme")
    await asyncio.wait_for(observer.entered.wait(), timeout=5)
    queued = await manager.send(env.project_id, AGENT, "Check readme again")
    assert queued.status == "queued"
    observer.release.set()

    first = (await collect_until_turn_ended(sub, timeout=10))[-1]
    second = (await collect_until_turn_ended(sub, timeout=10))[-1]

    assert isinstance(first, TurnEnded) and isinstance(second, TurnEnded)
    # One turn.ended for each turn: the fallback stored the first as done, its own status.
    assert first.data.turn_id != second.data.turn_id
    assert (first.data.status, second.data.status) == ("done", "done")
    for _ in range(2):
        await asyncio.wait_for(observer.ended.get(), timeout=10)
    assert [(c[0], c[4] if c[0] == "ended" else None) for c in observer.calls] == [
        ("started", None),
        ("ended", "done"),
        ("started", None),
        ("ended", "done"),
    ]
    assert await turn_statuses(databases, env.project_id) == ["done", "done"]
    assert "Could not store how turn" in caplog.text
    assert "disk full" in caplog.text

    await manager.close()


async def test_a_failing_fallback_is_logged_and_the_worker_ends_normally(
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
    end_writes: list[str] = []

    async def set_turn_failing_at_the_end(
        self: SessionStore, project_id: str, turn_id: str, status: str, *, error: str | None = None
    ) -> None:
        if status in ("done", "failed"):
            end_writes.append(status)
            raise RuntimeError(f"disk full writing {status}")
        await set_turn(self, project_id, turn_id, status, error=error)

    monkeypatch.setattr(SessionStore, "set_turn", set_turn_failing_at_the_end)
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
    await env.manager.send(env.project_id, AGENT, "Check readme")
    await asyncio.wait_for(observer.started.wait(), timeout=10)
    [worker] = observer.workers

    [outcome] = await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), timeout=10)

    # The end write and the fallback's one write, both as done; neither error is raised.
    assert outcome is None
    assert end_writes == ["done", "done"]
    assert await asyncio.wait_for(observer.ended.get(), timeout=10) == "done"
    assert "Could not record turn" in caplog.text
    await env.manager.close()


async def _escaping_run(
    self: TurnExecutor,
    project_id: str,
    session_id: str,
    agent: str,
    turn_id: str,
    sink: TurnSink,
    lock: AbstractAsyncContextManager[Any],
) -> tuple[TurnEnd, str | None]:
    raise RuntimeError("an error that escapes the executor")


async def _self_recording_run(
    self: TurnExecutor,
    project_id: str,
    session_id: str,
    agent: str,
    turn_id: str,
    sink: TurnSink,
    lock: AbstractAsyncContextManager[Any],
) -> tuple[TurnEnd, str | None]:
    # As `fail()` and `TurnRefused` do: the executor writes its own notice, then returns failed.
    await self.record_failure(project_id, session_id, turn_id, sink, lock, "model said no")
    return ("failed", "model said no")


@pytest.mark.parametrize(
    ("failing_run", "notice"),
    [
        pytest.param(_escaping_run, UNEXPECTED_FAILURE_TEXT, id="escaped-error"),
        pytest.param(_self_recording_run, "model said no", id="executor-recorded"),
    ],
)
async def test_the_fallback_writes_no_second_failure_notice(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    failing_run: Callable[..., Awaitable[tuple[TurnEnd, str | None]]],
    notice: str,
) -> None:
    set_turn = SessionStore.set_turn
    failures = ["disk full"]

    async def set_turn_failing_once(
        self: SessionStore, project_id: str, turn_id: str, status: str, *, error: str | None = None
    ) -> None:
        # The failure notice is written by then: the fallback must not write it again.
        if status == "failed" and failures:
            raise RuntimeError(failures.pop())
        await set_turn(self, project_id, turn_id, status, error=error)

    monkeypatch.setattr(TurnExecutor, "run", failing_run)
    monkeypatch.setattr(SessionStore, "set_turn", set_turn_failing_once)
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
    sub = env.manager.subscribe(env.project_id, AGENT, after_seq=None)
    await anext(sub)  # the opening resync
    await env.manager.send(env.project_id, AGENT, "Check readme")

    ended = (await collect_until_turn_ended(sub, timeout=10))[-1]
    assert await asyncio.wait_for(observer.ended.get(), timeout=10) == "failed"
    await env.manager.close()

    session_id = await env.store.session_id(env.project_id, AGENT)
    messages = await env.store.messages(env.project_id, session_id)
    assert [m.author_kind for m in messages] == ["user", "system"]
    assert messages[-1].parts == [TextPart(type="text", text=notice)]
    assert isinstance(ended, TurnEnded)
    # The stored end keeps the error the one notice shows.
    assert (ended.data.status, ended.data.error) == ("failed", notice)
    assert await turn_statuses(databases, env.project_id) == ["failed"]
    assert [(c[0], c[4]) for c in observer.calls if c[0] == "ended"] == [("ended", "failed")]


async def test_a_notice_whose_write_failed_is_written_by_the_fallback(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record_failure = TurnExecutor.record_failure
    failures = ["disk full"]

    async def record_failure_failing_once(
        self: TurnExecutor,
        project_id: str,
        session_id: str,
        turn_id: str,
        sink: TurnSink,
        lock: AbstractAsyncContextManager[Any],
        text: str,
    ) -> None:
        if failures:
            raise RuntimeError(failures.pop())
        await record_failure(self, project_id, session_id, turn_id, sink, lock, text)

    monkeypatch.setattr(TurnExecutor, "run", _escaping_run)
    monkeypatch.setattr(TurnExecutor, "record_failure", record_failure_failing_once)
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
    await env.manager.send(env.project_id, AGENT, "Check readme")
    assert await asyncio.wait_for(observer.ended.get(), timeout=10) == "failed"
    await env.manager.close()

    session_id = await env.store.session_id(env.project_id, AGENT)
    messages = await env.store.messages(env.project_id, session_id)
    assert [m.author_kind for m in messages] == ["user", "system"]
    assert messages[-1].parts == [TextPart(type="text", text=UNEXPECTED_FAILURE_TEXT)]
    assert await turn_statuses(databases, env.project_id) == ["failed"]
