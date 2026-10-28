"""A done, stopped or failed turn whose end and fallback writes both fail is still reported
ended, once, with its own status, and the queue goes on (Decision #249)."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import ErrorKind, TurnEnd, TurnEnded
from mlops_agents.agents.runtime.executor import TurnExecutor, TurnOutcome
from mlops_agents.agents.runtime.graph import TurnSink
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.observer_fakes import HeldObserver, WorkerCapturingObserver
from tests.agents.runtime.session_env import (
    build_session_env,
    collect_until_turn_ended,
    opened_stream,
    turn_ended_statuses,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

AGENT = "orchestrator"
END_STATUSES = ("done", "stopped", "failed")


@dataclass
class FailingEndWrites:
    """Every end write of the first turn fails, the fallback's too; other writes go through.
    The first such write can be held until the test releases it."""

    first_turn: list[str] = field(default_factory=lambda: [])
    attempts: list[str] = field(default_factory=lambda: [])
    hold: bool = False
    held: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


def fail_the_first_turns_end_writes(
    monkeypatch: pytest.MonkeyPatch, *, hold: bool = False
) -> FailingEndWrites:
    writes = FailingEndWrites(hold=hold)
    set_turn = SessionStore.set_turn

    async def failing_set_turn(
        self: SessionStore,
        project_id: str,
        turn_id: str,
        status: str,
        *,
        error: str | None = None,
        error_kind: ErrorKind | None = None,
    ) -> None:
        if status == "running" and not writes.first_turn:
            writes.first_turn.append(turn_id)
        if writes.first_turn == [turn_id] and status in END_STATUSES:
            writes.attempts.append(status)
            if writes.hold and not writes.held.is_set():
                writes.held.set()
                await writes.release.wait()
            raise RuntimeError(f"disk full writing {status}")
        await set_turn(self, project_id, turn_id, status, error=error, error_kind=error_kind)

    monkeypatch.setattr(SessionStore, "set_turn", failing_set_turn)
    return writes


def fail_the_first_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first turn's run raises, so the worker ends it failed and writes its notice."""
    run = TurnExecutor.run
    failures = ["an error that escapes the executor"]

    async def failing_once(
        self: TurnExecutor,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str,
        sink: TurnSink,
        lock: AbstractAsyncContextManager[Any],
    ) -> TurnOutcome:
        if failures:
            raise RuntimeError(failures.pop())
        return await run(self, project_id, session_id, agent, turn_id, sink, lock)

    monkeypatch.setattr(TurnExecutor, "run", failing_once)


@pytest.mark.parametrize("end", END_STATUSES)
async def test_a_failing_end_and_fallback_still_report_the_end_once_and_the_queue_goes_on(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    end: TurnEnd,
) -> None:
    writes = fail_the_first_turns_end_writes(monkeypatch)
    if end == "failed":
        fail_the_first_run(monkeypatch)
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
    sub = await opened_stream(env)
    await env.manager.send(env.project_id, AGENT, "Check readme")
    await asyncio.wait_for(observer.entered.wait(), timeout=5)
    queued = await env.manager.send(env.project_id, AGENT, "Check readme again")
    assert queued.status == "queued"
    if end == "stopped":
        # Held in turn_started, the turn is stopped before its task exists.
        await env.manager.stop(env.project_id, AGENT)
    observer.release.set()

    events = await collect_until_turn_ended(sub, timeout=10)
    events += await collect_until_turn_ended(sub, timeout=10)
    for _ in range(2):
        await asyncio.wait_for(observer.ended.get(), timeout=10)

    [first_turn] = writes.first_turn
    ended = [e for e in events if isinstance(e, TurnEnded)]
    # One turn.ended per turn: the first keeps its own status, the second ran as usual.
    assert [(e.data.turn_id == first_turn, e.data.status) for e in ended] == [
        (True, end),
        (False, "done"),
    ]
    assert [(c[0], c[4] if c[0] == "ended" else None) for c in observer.calls] == [
        ("started", None),
        ("ended", end),
        ("started", None),
        ("ended", "done"),
    ]
    # The end write and the fallback's one write, both as the end's own status.
    assert writes.attempts == [end, end]
    assert "Could not store how turn" in caplog.text
    assert f"Could not record turn {first_turn}" in caplog.text
    session_id = await env.store.session_id(env.project_id, AGENT)
    notices = [
        m
        for m in await env.store.messages(env.project_id, session_id)
        if m.author_kind == "system" and m.turn_id == first_turn
    ]
    assert len(notices) == (1 if end == "failed" else 0)
    await env.manager.close()


async def test_a_close_during_a_failing_end_ends_the_worker_with_its_cancel(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = fail_the_first_turns_end_writes(monkeypatch, hold=True)
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
    await asyncio.wait_for(writes.held.wait(), timeout=10)
    [worker] = observer.workers

    close = asyncio.create_task(env.manager.close())
    # The close cancels the workers before its first suspension.
    await asyncio.sleep(0)
    assert worker.cancelling() == 1
    writes.release.set()
    await asyncio.wait_for(close, timeout=10)

    # The close's cancel ends the worker once the end is reported, not the storage error.
    assert worker.cancelled()
    assert writes.attempts == ["done", "done"]
    assert await turn_ended_statuses(sub) == ["done"]
    assert [(c[0], c[4]) for c in observer.calls if c[0] == "ended"] == [("ended", "done")]
