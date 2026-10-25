"""A failed turn gets exactly one failure notice, also when publishing the notice fails."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import MessageView, TextPart, TurnEnd, TurnEnded
from mlops_agents.agents.runtime.executor import UNEXPECTED_FAILURE_TEXT, TurnExecutor
from mlops_agents.agents.runtime.graph import TurnSink
from mlops_agents.agents.runtime.hub import SessionHub
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.observer_fakes import HeldObserver, turn_statuses
from tests.agents.runtime.session_env import (
    build_session_env,
    collect_until_turn_ended,
    opened_stream,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

AGENT = "orchestrator"


async def _escaping(
    executor: TurnExecutor,
    project_id: str,
    session_id: str,
    turn_id: str,
    sink: TurnSink,
    lock: AbstractAsyncContextManager[Any],
) -> None:
    # The worker writes the notice of an error that escapes the executor.
    raise RuntimeError("an error that escapes the executor")


async def _self_recording(
    executor: TurnExecutor,
    project_id: str,
    session_id: str,
    turn_id: str,
    sink: TurnSink,
    lock: AbstractAsyncContextManager[Any],
) -> None:
    # As `fail()` does: the executor writes its own notice, whose publish fails here.
    await executor.record_failure(project_id, session_id, turn_id, sink, lock, "model said no")


def fail_the_first_run(
    monkeypatch: pytest.MonkeyPatch, failing: Callable[..., Awaitable[None]]
) -> None:
    run_turn = TurnExecutor.run
    runs: list[str] = []

    async def failing_once(
        self: TurnExecutor,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str,
        sink: TurnSink,
        lock: AbstractAsyncContextManager[Any],
    ) -> tuple[TurnEnd, str | None]:
        runs.append(turn_id)
        if len(runs) == 1:
            await failing(self, project_id, session_id, turn_id, sink, lock)
            raise AssertionError("unreachable: the failing run raises")
        return await run_turn(self, project_id, session_id, agent, turn_id, sink, lock)

    monkeypatch.setattr(TurnExecutor, "run", failing_once)


def fail_the_first_notice_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first publish of a new system message fails after the message is stored."""
    message = SessionHub.message
    failures = ["the publish failed"]

    async def failing_once(self: SessionHub, msg: MessageView, *, created: bool) -> None:
        if created and msg.author_kind == "system" and failures:
            raise RuntimeError(failures.pop())
        await message(self, msg, created=created)

    monkeypatch.setattr(SessionHub, "message", failing_once)


@pytest.mark.parametrize(
    ("failing", "notice"),
    [
        pytest.param(_escaping, UNEXPECTED_FAILURE_TEXT, id="worker-writes-the-notice"),
        pytest.param(_self_recording, "model said no", id="executor-writes-the-notice"),
    ],
)
async def test_a_failing_notice_publish_leaves_one_notice_and_the_next_turn_runs(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    failing: Callable[..., Awaitable[None]],
    notice: str,
) -> None:
    fail_the_first_run(monkeypatch, failing)
    fail_the_first_notice_publish(monkeypatch)
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
    observer.release.set()

    first = (await collect_until_turn_ended(sub, timeout=10))[-1]
    second = (await collect_until_turn_ended(sub, timeout=10))[-1]

    assert isinstance(first, TurnEnded) and isinstance(second, TurnEnded)
    assert first.data.turn_id != second.data.turn_id
    assert (first.data.status, second.data.status) == ("failed", "done")
    for _ in range(2):
        await asyncio.wait_for(observer.ended.get(), timeout=10)
    assert [(c[0], c[4]) for c in observer.calls if c[0] == "ended"] == [
        ("ended", "failed"),
        ("ended", "done"),
    ]
    session_id = await env.store.session_id(env.project_id, AGENT)
    notices = [
        m for m in await env.store.messages(env.project_id, session_id) if m.author_kind == "system"
    ]
    assert [(m.turn_id, m.parts) for m in notices] == [
        (first.data.turn_id, [TextPart(type="text", text=notice)])
    ]
    assert sorted(await turn_statuses(databases, env.project_id)) == ["done", "failed"]

    await env.manager.close()
