"""The per-session worker tasks that run a session's queued turns one at a time."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from mlops_agents.agents.runtime.contract import TurnEnd
from mlops_agents.agents.runtime.executor import TurnExecutor
from mlops_agents.agents.runtime.hooks import TurnObserver
from mlops_agents.agents.runtime.hub import AsyncRLock, SessionHub
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.runtime.turn_end import UNEXPECTED_FAILURE, Ending, TurnEnds, observed

logger = logging.getLogger(__name__)

__all__ = ["TurnWorkers"]

SessionKey = tuple[str, str]


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
        self._ends = TurnEnds(store=store, executor=executor, observers=self._observers)
        self._workers: dict[SessionKey, asyncio.Task[None]] = {}
        self._child_tasks: dict[SessionKey, asyncio.Task[tuple[TurnEnd, str | None]]] = {}
        # The turn each worker is on, from the moment it takes it, and the ones Stop ended.
        self._current_turns: dict[SessionKey, str] = {}
        self._stopped_turns: set[str] = set()
        # The workers that have not looked for their first turn yet, and the Stops that came
        # in that time (right after the Send that started the worker): such a Stop is for the
        # first turn, and dropped if the worker finds none. A Stop between two turns has no
        # turn to stop and is dropped, as in stage 1.
        self._first_look: set[SessionKey] = set()
        self._stop_next: set[SessionKey] = set()

    def running(self, key: SessionKey) -> bool:
        worker = self._workers.get(key)
        return worker is not None and not worker.done()

    def start(
        self, project_id: str, session_id: str, agent: str, hub: SessionHub, lock: AsyncRLock
    ) -> None:
        """Start the session's worker. The caller holds the session lock and saw none running."""
        key = (project_id, session_id)
        self._first_look.add(key)
        self._workers[key] = asyncio.create_task(
            self._loop(project_id, session_id, agent, hub, lock)
        )

    def stop(self, key: SessionKey) -> None:
        """Stop the turn of a running worker: the one it is on, or, before its first look, the
        one it is about to take. A turn stopped before its task exists ends stopped without
        running. With no worker running, or between two turns, there is nothing to stop."""
        if not self.running(key):
            return
        turn_id = self._current_turns.get(key)
        if turn_id is None:
            if key in self._first_look:
                self._stop_next.add(key)
            return
        self._stopped_turns.add(turn_id)
        child = self._child_tasks.get(key)
        if child is not None and not child.done():
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
        for key in list(self._current_turns.keys()):
            if project_id in (None, key[0]):
                self._current_turns.pop(key, None)
        self._first_look = {k for k in self._first_look if project_id not in (None, k[0])}
        self._stop_next = {k for k in self._stop_next if project_id not in (None, k[0])}
        if project_id is None:
            self._stopped_turns.clear()

    async def _loop(
        self, project_id: str, session_id: str, agent: str, hub: SessionHub, lock: AsyncRLock
    ) -> None:
        key = (project_id, session_id)

        try:
            while True:
                async with lock:
                    next_turn = await self._store.next_queued_turn(project_id, session_id)
                    # Taking the turn and claiming an early Stop happen with no await between
                    # them, so a Stop lands either here or on the turn itself.
                    self._first_look.discard(key)
                    if next_turn is None:
                        self._stop_next.discard(key)
                        self._workers.pop(key, None)
                        break
                    turn_id, trigger_msg_id = next_turn
                    self._current_turns[key] = turn_id
                    if key in self._stop_next:
                        self._stop_next.discard(key)
                        self._stopped_turns.add(turn_id)

                try:
                    await self._take_turn(
                        project_id, session_id, agent, turn_id, trigger_msg_id, hub, lock
                    )
                finally:
                    self._current_turns.pop(key, None)
                    self._stopped_turns.discard(turn_id)

        finally:
            self._first_look.discard(key)
            self._stop_next.discard(key)
            self._workers.pop(key, None)

    async def _take_turn(
        self,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str,
        trigger_msg_id: str,
        hub: SessionHub,
        lock: AsyncRLock,
    ) -> None:
        try:
            await self._store.set_turn(project_id, turn_id, "running")
            await self._turn_started(project_id, agent, turn_id)
            await self._ends.settle_trigger(
                project_id, session_id, turn_id, trigger_msg_id, hub, lock, read=True
            )
            ending = await self._run_turn(project_id, session_id, agent, turn_id, hub, lock)
        except asyncio.CancelledError:
            # The turn was taken, so it is reported ended, also when the cancel came first.
            interrupted = Ending("interrupted")
            await self._ends.end(
                project_id, session_id, agent, turn_id, trigger_msg_id, hub, lock, interrupted
            )
            raise
        # Nothing suspends between deciding the end and ending: a cancel from here on lands in
        # the shielded ending, which keeps the decided status and reports it once.
        await self._ends.end(
            project_id, session_id, agent, turn_id, trigger_msg_id, hub, lock, ending
        )
        current = asyncio.current_task()
        if current is not None and current.cancelling() > 0:
            # The worker's cancel came in the pass its turn task ended (see `_run_turn`).
            raise asyncio.CancelledError

    async def _run_turn(
        self,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str,
        hub: SessionHub,
        lock: AsyncRLock,
    ) -> Ending:
        """Run the turn task and decide how it ended; `TurnEnds.end` stores it.

        A cancel of the turn task other than by Stop, or of the worker before the turn task
        ended, is raised for an interrupted end. A cancel of the worker that comes once the turn
        task has ended, by itself or by Stop, keeps that end; `_take_turn` raises it after."""
        key = (project_id, session_id)
        if turn_id in self._stopped_turns:
            return Ending("stopped")
        child_task = asyncio.create_task(
            self._executor.run(project_id, session_id, agent, turn_id, hub, lock=lock)
        )
        self._child_tasks[key] = child_task
        try:
            await child_task
            return _ending_of(turn_id, child_task)
        except asyncio.CancelledError:
            # The turn task has ended here: a cancel of the worker while it waits is forwarded
            # to the turn task, and the wait ends only with it.
            current = asyncio.current_task()
            if current is not None and current.cancelling() > 0:
                # The worker itself is being cancelled (close / close_project).
                if child_task.cancelling() == 0 and not child_task.cancelled():
                    # The turn task ended in the pass the worker's cancel came in, so the wait
                    # raised the cancel all the same: its end stands, and `_take_turn` raises
                    # the cancel once that end is stored.
                    return _ending_of(turn_id, child_task)
                if child_task.cancelled() and turn_id in self._stopped_turns:
                    # A Stop was requested and the turn task ended cancelled before the
                    # worker woke: the Stop stands.
                    return Ending("stopped")
                raise
            if turn_id not in self._stopped_turns:
                raise
            return Ending("stopped")
        except Exception:
            return _ending_of(turn_id, child_task)
        finally:
            self._child_tasks.pop(key, None)

    async def _turn_started(self, project_id: str, agent: str, turn_id: str) -> None:
        for observer in self._observers:
            await observed(observer.turn_started(project_id, agent, turn_id))


def _ending_of(turn_id: str, child_task: asyncio.Task[tuple[TurnEnd, str | None]]) -> Ending:
    """How a finished, not cancelled turn task ended; an error that escaped it fails the turn."""
    error = child_task.exception()
    if error is not None:
        logger.error("Turn %s failed outside the executor", turn_id, exc_info=error)
        return UNEXPECTED_FAILURE
    end_status, end_error = child_task.result()
    return Ending(end_status, end_error)
