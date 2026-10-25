"""Keeping each turn task, so a test can cancel one the way neither Stop nor a close does."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest

from mlops_agents.agents.runtime.contract import TurnEnd
from mlops_agents.agents.runtime.executor import TurnExecutor
from mlops_agents.agents.runtime.graph import TurnSink


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
    ) -> tuple[TurnEnd, str | None]:
        task = asyncio.current_task()
        assert task is not None
        turn_tasks.append(task)
        return await run_turn(self, project_id, session_id, agent, turn_id, sink, lock)

    monkeypatch.setattr(TurnExecutor, "run", recording_run)
    return turn_tasks
