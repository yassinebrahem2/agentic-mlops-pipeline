"""The per-session worker tasks that run a session's queued turns one at a time."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Sequence

from mlops_agents.agents.runtime.contract import TextPart, TurnEnd
from mlops_agents.agents.runtime.executor import UNEXPECTED_FAILURE_TEXT, TurnExecutor
from mlops_agents.agents.runtime.hooks import TurnObserver
from mlops_agents.agents.runtime.hub import AsyncRLock, SessionHub
from mlops_agents.agents.runtime.shielded import shielded
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
        await self._store.set_turn(project_id, turn_id, "running")
        try:
            await self._turn_started(project_id, agent, turn_id)
            await self._settle_trigger(
                project_id, session_id, turn_id, trigger_msg_id, hub, lock, read=True
            )
            ended = await self._run_turn(project_id, session_id, agent, turn_id, hub, lock)
        except asyncio.CancelledError:
            # The turn started, so it is reported ended, also when the cancel came first.
            await self._store.set_turn(project_id, turn_id, "interrupted")
            await self._store.mark_interrupted(project_id, turn_id)
            # A cancel before the trigger was read would leave it queued for good.
            await self._settle_trigger(
                project_id, session_id, turn_id, trigger_msg_id, hub, lock, read=False
            )
            await self._turn_ended(project_id, session_id, agent, turn_id, "interrupted")
            raise
        # The observers hear of the end after the turn's own handling, so a cancel that
        # lands in an observer never rewrites how the turn ended, nor reports it twice.
        await self._turn_ended(project_id, session_id, agent, turn_id, ended)

    async def _settle_trigger(
        self,
        project_id: str,
        session_id: str,
        turn_id: str,
        trigger_msg_id: str,
        hub: SessionHub,
        lock: AsyncRLock,
        *,
        read: bool,
    ) -> None:
        """A queued trigger becomes complete. When the turn `read` it and it is from an agent,
        it records this turn read it; a turn interrupted before reading it records nothing."""
        async with lock:
            trigger = await self._store.message(project_id, session_id, trigger_msg_id)
            from_agent = trigger is not None and trigger.from_agent is not None
            read_by = turn_id if read and from_agent else None
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
        if turn_id in self._stopped_turns:
            return await self._end_stopped(project_id, turn_id, hub)
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
            if turn_id not in self._stopped_turns:
                raise
            return await self._end_stopped(project_id, turn_id, hub)
        except Exception:
            logger.exception("Turn %s failed outside the executor", turn_id)
            await self._executor.record_failure(
                project_id, session_id, turn_id, hub, lock, UNEXPECTED_FAILURE_TEXT
            )
            await self._store.set_turn(project_id, turn_id, "failed", error=UNEXPECTED_FAILURE_TEXT)
            await hub.turn_ended(turn_id, status="failed", error=UNEXPECTED_FAILURE_TEXT)
            return "failed"
        finally:
            self._child_tasks.pop(key, None)

    async def _end_stopped(self, project_id: str, turn_id: str, hub: SessionHub) -> TurnEnd:
        await self._store.set_turn(project_id, turn_id, "stopped")
        await hub.turn_ended(turn_id, status="stopped", error=None)
        return "stopped"

    async def _turn_started(self, project_id: str, agent: str, turn_id: str) -> None:
        for observer in self._observers:
            await _observed(observer.turn_started(project_id, agent, turn_id))

    @shielded
    async def _turn_ended(
        self, project_id: str, session_id: str, agent: str, turn_id: str, status: TurnEnd
    ) -> None:
        """Every observer hears the end. A cancel of the worker that lands here waits for the
        last of them (each is bounded by OBSERVER_TIMEOUT_S), then goes on to the worker."""
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
        message = await self._store.turn_agent_message(project_id, session_id, turn_id)
        if message is None:
            return None
        texts = [part.text for part in message.parts if isinstance(part, TextPart) and part.text]
        return texts[-1] if texts else None


async def _observed(call: Awaitable[None]) -> None:
    """Await one observer call, bounded. A failing or hanging observer is logged and never
    stops the queue; a hanging one holds a shutdown at most OBSERVER_TIMEOUT_S."""
    try:
        async with asyncio.timeout(OBSERVER_TIMEOUT_S):
            await call
    except asyncio.CancelledError:
        # Only a cancel of the worker itself goes on; an observer's own cancel is its failure.
        current = asyncio.current_task()
        if current is not None and current.cancelling() > 0:
            raise
        logger.exception("A turn observer was cancelled")
    except Exception:
        logger.exception("A turn observer failed")
