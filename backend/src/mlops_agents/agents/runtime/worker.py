"""The per-session worker tasks that run a session's queued turns one at a time."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Sequence
from typing import Any

from mlops_agents.agents.runtime.contract import TextPart, TurnEnd
from mlops_agents.agents.runtime.executor import UNEXPECTED_FAILURE_TEXT, TurnExecutor
from mlops_agents.agents.runtime.hooks import TurnObserver
from mlops_agents.agents.runtime.hub import AsyncRLock, SessionHub
from mlops_agents.agents.runtime.store import SessionStore

logger = logging.getLogger(__name__)

__all__ = ["TurnWorkers"]

SessionKey = tuple[str, str]

# How long one observer call may take before it is abandoned and logged.
OBSERVER_TIMEOUT_S = 10.0


class TurnWorkers:
    """Owns each session's worker task and the turn task it is running."""

    def __init__(
        self,
        *,
        store: SessionStore,
        executor: TurnExecutor,
        observers: Sequence[TurnObserver],
    ) -> None:
        self._store = store
        self._executor = executor
        self._observers = tuple(observers)
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
                try:
                    await self._turn_started(project_id, agent, turn_id)
                    await self._mark_trigger_read(
                        project_id, session_id, turn_id, trigger_msg_id, hub, lock
                    )
                    ended = await self._run_turn(project_id, session_id, agent, turn_id, hub, lock)
                except asyncio.CancelledError:
                    # The turn started, so it is reported ended, also when the cancel came first.
                    await self._store.set_turn(project_id, turn_id, "interrupted")
                    await self._store.mark_interrupted(project_id, turn_id)
                    await self._turn_ended(project_id, session_id, agent, turn_id, "interrupted")
                    raise
                # The observers hear of the end after the turn's own handling, so a cancel that
                # lands in an observer never rewrites how the turn ended, nor reports it twice.
                await self._turn_ended(project_id, session_id, agent, turn_id, ended)

        finally:
            self._workers.pop(key, None)

    async def _mark_trigger_read(
        self,
        project_id: str,
        session_id: str,
        turn_id: str,
        trigger_msg_id: str,
        hub: SessionHub,
        lock: AsyncRLock,
    ) -> None:
        """A queued trigger becomes complete; one from an agent records this turn read it."""
        async with lock:
            history = await self._store.messages(project_id, session_id)
            trigger = next((m for m in history if m.id == trigger_msg_id), None)
            read_by = turn_id if trigger is not None and trigger.from_agent is not None else None
            if trigger is not None and trigger.status == "queued":
                updated_msg = await self._store.update_message(
                    project_id, trigger_msg_id, status="complete", read_by_turn=read_by
                )
                await hub.message(updated_msg, created=False)
            elif read_by is not None:
                await self._store.update_message(project_id, trigger_msg_id, read_by_turn=read_by)

    async def _run_turn(
        self,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str,
        hub: SessionHub,
        lock: AsyncRLock,
    ) -> TurnEnd:
        """Run the turn task and store how it ended. A cancel of the worker, or of the turn
        task other than by Stop, is raised for the caller to mark the turn interrupted."""
        key = (project_id, session_id)
        child_task = asyncio.create_task(
            self._executor.run(project_id, session_id, agent, turn_id, hub, lock=lock)
        )
        self._child_tasks[key] = child_task
        try:
            end_status, error = await child_task
            await self._store.set_turn(project_id, turn_id, end_status, error=error)
            await hub.turn_ended(turn_id, status=end_status, error=error)
            return end_status
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
                raise
            if child_task not in self._stopped_tasks:
                raise
            self._stopped_tasks.discard(child_task)
            await self._store.set_turn(project_id, turn_id, "stopped")
            await hub.turn_ended(turn_id, status="stopped", error=None)
            return "stopped"
        except Exception:
            logger.exception("Turn %s failed outside the executor", turn_id)
            await self._executor.record_failure(
                project_id, session_id, turn_id, hub, lock, UNEXPECTED_FAILURE_TEXT
            )
            await self._store.set_turn(project_id, turn_id, "failed", error=UNEXPECTED_FAILURE_TEXT)
            await hub.turn_ended(turn_id, status="failed", error=UNEXPECTED_FAILURE_TEXT)
            return "failed"
        finally:
            self._stopped_tasks.discard(child_task)
            self._child_tasks.pop(key, None)

    async def _turn_started(self, project_id: str, agent: str, turn_id: str) -> None:
        for observer in self._observers:
            await _observed(observer.turn_started(project_id, agent, turn_id))

    async def _turn_ended(
        self, project_id: str, session_id: str, agent: str, turn_id: str, status: TurnEnd
    ) -> None:
        if not self._observers:
            return
        try:
            final_text = await self._final_text(project_id, session_id, turn_id)
        except Exception:
            # The turn has ended either way; the observers still hear of it.
            logger.exception("Could not read the final text of turn %s", turn_id)
            final_text = None
        for observer in self._observers:
            await _observed(observer.turn_ended(project_id, agent, turn_id, status, final_text))

    async def _final_text(self, project_id: str, session_id: str, turn_id: str) -> str | None:
        """The last text the agent wrote in the turn, or None when it wrote none."""
        texts = [
            part.text
            for message in await self._store.messages(project_id, session_id)
            if message.turn_id == turn_id and message.author_kind == "agent"
            for part in message.parts
            if isinstance(part, TextPart) and part.text
        ]
        return texts[-1] if texts else None


async def _observed(call: Awaitable[None]) -> None:
    """Await one observer call, bounded. A failing or hanging observer is logged and never
    stops the queue, nor holds a shutdown that is cancelling the worker."""
    try:
        async with asyncio.timeout(OBSERVER_TIMEOUT_S):
            await call
    except Exception:
        logger.exception("A turn observer failed")
