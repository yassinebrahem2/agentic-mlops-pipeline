"""Keeping each turn task, so a test can cancel one the way neither Stop nor a close does."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest

from mlops_agents.agents.runtime.contract import TurnEnded
from mlops_agents.agents.runtime.executor import TurnExecutor, TurnOutcome
from mlops_agents.agents.runtime.graph import TurnSink
from tests.agents.runtime.observer_fakes import WorkerCapturingObserver
from tests.agents.runtime.session_env import SessionEnv, collect_until_turn_ended, opened_stream
from tests.fakes.fake_openai import RunningFake
from tests.stream_helpers import through_first_delta

AGENT = "orchestrator"


def record_turn_tasks(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.Task[Any]]:
    """The task of every turn the executor runs, in the order they start."""
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
    return turn_tasks


async def interrupt_the_first_of_two_turns(
    env: SessionEnv,
    fake_openai: RunningFake,
    monkeypatch: pytest.MonkeyPatch,
    observer: WorkerCapturingObserver,
) -> None:
    """Cancel the first turn's task, by neither Stop nor a close, while a second is queued.

    The first must end interrupted, published and reported, and the same worker must then run
    the second to its end and finish once the queue is empty, not by the cancel.
    """
    turn_tasks = record_turn_tasks(monkeypatch)
    await env.settings.set_default_model(AGENT, f"{env.provider}:fake-slow")
    fake_openai.state.hold_slow()
    sub = await opened_stream(env)
    await env.manager.send(env.project_id, AGENT, "Slow response")
    queued = await env.manager.send(env.project_id, AGENT, "Slow response again")
    assert queued.status == "queued"
    await through_first_delta(sub)

    turn_tasks[0].cancel()
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
    first_worker, second_worker = observer.workers
    assert first_worker is second_worker
    await asyncio.wait_for(asyncio.gather(first_worker, return_exceptions=True), timeout=10)
    assert not first_worker.cancelled() and first_worker.exception() is None
