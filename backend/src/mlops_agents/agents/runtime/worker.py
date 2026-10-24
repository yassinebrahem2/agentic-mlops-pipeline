"""The per-session worker tasks that run a session's queued turns one at a time."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mlops_agents.agents.runtime.contract import TurnEnd
from mlops_agents.agents.runtime.executor import UNEXPECTED_FAILURE_TEXT, TurnExecutor
from mlops_agents.agents.runtime.hub import AsyncRLock, SessionHub
from mlops_agents.agents.runtime.store import SessionStore

logger = logging.getLogger(__name__)

__all__ = ["TurnWorkers"]

SessionKey = tuple[str, str]


class TurnWorkers:
    """Owns each session's worker task and the turn task it is running."""

    def __init__(self, *, store: SessionStore, executor: TurnExecutor) -> None:
        self._store = store
        self._executor = executor
        self._workers: dict[SessionKey, asyncio.Task[None]] = {}
        self._child_tasks: dict[SessionKey, asyncio.Task[tuple[TurnEnd, str | None]]] = {}
        self._stopped_tasks: set[asyncio.Task[Any]] = set()

    def running(self, key: SessionKey) -> bool:
        worker = self._workers.get(key)
        return worker is not None and not worker.done()

    def start(
        self, project_id: str, session_id: str, agent: str, hub: SessionHub, lock: AsyncRLock
    ) -> None:
        """Start the session's worker. The caller holds the session lock and saw none running."""
        self._workers[(project_id, session_id)] = asyncio.create_task(
            self._loop(project_id, session_id, agent, hub, lock)
        )

    def stop(self, key: SessionKey) -> None:
        if self.running(key):
            child = self._child_tasks.get(key)
            if child is not None and not child.done():
                self._stopped_tasks.add(child)
                child.cancel()

    def projects(self) -> set[str]:
        """The projects with a worker or a turn task."""
        return {p for (p, _) in self._workers} | {p for (p, _) in self._child_tasks}

    async def cancel(self, project_id: str | None = None) -> None:
        """Cancel the workers, then the turn tasks, of one project or of all; wait for both."""
        workers = [t for (p, _), t in list(self._workers.items()) if project_id in (None, p)]
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

        children = [t for (p, _), t in list(self._child_tasks.items()) if project_id in (None, p)]
        for task in children:
            task.cancel()
        if children:
            await asyncio.gather(*children, return_exceptions=True)

    def forget(self, project_id: str | None = None) -> None:
        """Drop the references of one project, or of all."""
        for key in list(self._workers.keys()):
            if project_id in (None, key[0]):
                self._workers.pop(key, None)
        for key in list(self._child_tasks.keys()):
            if project_id in (None, key[0]):
                self._child_tasks.pop(key, None)
        if project_id is None:
            self._stopped_tasks.clear()

    async def _loop(
        self, project_id: str, session_id: str, agent: str, hub: SessionHub, lock: AsyncRLock
    ) -> None:
        key = (project_id, session_id)

        try:
            while True:
                async with lock:
                    next_turn = await self._store.next_queued_turn(project_id, session_id)
                    if next_turn is None:
                        self._workers.pop(key, None)
                        break

                turn_id, trigger_msg_id = next_turn
                await self._store.set_turn(project_id, turn_id, "running")

                # If trigger message was queued, update it to complete
                async with lock:
                    history = await self._store.messages(project_id, session_id)
                    triggers = [m for m in history if m.id == trigger_msg_id]
                    if triggers and triggers[0].status == "queued":
                        updated_msg = await self._store.update_message(
                            project_id, trigger_msg_id, status="complete"
                        )
                        await hub.message(updated_msg, created=False)

                child_task = asyncio.create_task(
                    self._executor.run(project_id, session_id, agent, turn_id, hub, lock=lock)
                )
                self._child_tasks[key] = child_task

                try:
                    try:
                        end_status, error = await child_task
                        await self._store.set_turn(project_id, turn_id, end_status, error=error)
                        await hub.turn_ended(turn_id, status=end_status, error=error)
                    except asyncio.CancelledError:
                        current = asyncio.current_task()
                        if current is not None and current.cancelling() > 0:
                            # The worker itself is being cancelled (close / close_project).
                            # Do not swallow worker cancellation!
                            if not child_task.done():
                                child_task.cancel()
                                try:
                                    await child_task
                                except asyncio.CancelledError:
                                    pass
                                except Exception:
                                    logger.exception("Turn %s failed while closing", turn_id)
                            await self._store.set_turn(project_id, turn_id, "interrupted")
                            await self._store.mark_interrupted(project_id, turn_id)
                            raise
                        if child_task in self._stopped_tasks:
                            self._stopped_tasks.discard(child_task)
                            await self._store.set_turn(project_id, turn_id, "stopped")
                            await hub.turn_ended(turn_id, status="stopped", error=None)
                            continue
                        await self._store.set_turn(project_id, turn_id, "interrupted")
                        await self._store.mark_interrupted(project_id, turn_id)
                        raise
                    except Exception:
                        logger.exception("Turn %s failed outside the executor", turn_id)
                        await self._executor.record_failure(
                            project_id, session_id, turn_id, hub, lock, UNEXPECTED_FAILURE_TEXT
                        )
                        await self._store.set_turn(
                            project_id, turn_id, "failed", error=UNEXPECTED_FAILURE_TEXT
                        )
                        await hub.turn_ended(
                            turn_id, status="failed", error=UNEXPECTED_FAILURE_TEXT
                        )
                finally:
                    self._stopped_tasks.discard(child_task)
                    self._child_tasks.pop(key, None)

        finally:
            self._workers.pop(key, None)
