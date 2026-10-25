"""The per-session worker tasks that run a session's queued turns one at a time."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, replace
from typing import Literal, cast

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


@dataclass(frozen=True)
class _Ending:
    """How a taken turn ended, decided before anything of it is stored. `failure_notice` is
    the system message of a failure still to be written: None when there is none left to
    write (no failure, written already, or recorded by the executor itself)."""

    status: TurnEnd | Literal["waiting"]
    error: str | None = None
    failure_notice: str | None = None

    @property
    def notice_recorded(self) -> bool:
        """Whether this is a failed end whose failure notice is stored."""
        return self.status == "failed" and self.failure_notice is None


# A failure no part of the turn recorded: its system message is written with the end.
_UNEXPECTED_FAILURE = _Ending("failed", UNEXPECTED_FAILURE_TEXT, UNEXPECTED_FAILURE_TEXT)


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
                    should_continue = await self._take_turn(
                        project_id, session_id, agent, turn_id, trigger_msg_id, hub, lock
                    )
                    if not should_continue:
                        break
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
    ) -> bool:
        try:
            await self._store.set_turn(project_id, turn_id, "running")
            await self._turn_started(project_id, agent, turn_id)
            await self._settle_trigger(
                project_id, session_id, turn_id, trigger_msg_id, hub, lock, read=True
            )
            ending = await self._run_turn(project_id, session_id, agent, turn_id, hub, lock)
        except asyncio.CancelledError:
            # The turn was taken, so it is reported ended, also when the cancel came first.
            interrupted = _Ending("interrupted")
            await self._end_turn(
                project_id, session_id, agent, turn_id, trigger_msg_id, hub, lock, interrupted
            )
            raise

        if ending.status == "waiting":
            await self._store.set_turn(project_id, turn_id, "waiting")
            return False

        # Nothing suspends between deciding the end and ending: a cancel from here on lands in
        # the shielded ending, which keeps the decided status and reports it once.
        await self._end_turn(
            project_id, session_id, agent, turn_id, trigger_msg_id, hub, lock, ending
        )
        current = asyncio.current_task()
        if current is not None and current.cancelling() > 0:
            # The worker's cancel came in the pass its turn task ended (see `_run_turn`).
            raise asyncio.CancelledError
        return True

    @shielded
    async def _end_turn(
        self,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str,
        trigger_msg_id: str,
        hub: SessionHub,
        lock: AsyncRLock,
        ending: _Ending,
    ) -> None:
        """The one owner of a taken turn's end: its stored end state, then the report to every
        observer. It runs whole: a cancel of the worker (close, close_project, or both) waits
        for it and is raised after, so it never rewrites the end nor reports it twice.

        It runs in its own task and takes the session lock, which the worker never holds when
        it gets here, and the turn task has finished by then.
        """
        if ending.status == "waiting":
            return

        if ending.status == "interrupted":
            await self._store.set_turn(project_id, turn_id, "interrupted")
            await self._store.mark_interrupted(project_id, turn_id)
            # A cancel before the trigger was read would leave it queued for good.
            await self._settle_trigger(
                project_id, session_id, turn_id, trigger_msg_id, hub, lock, read=False
            )
        else:
            try:
                ending = await self._write_notice(
                    project_id, session_id, turn_id, hub, lock, ending
                )
                await self._store_end(project_id, turn_id, hub, ending)
            except Exception:
                # As any other unexpected failure of the turn: it ends failed and the queue goes
                # on, with one failure notice. A failed end whose notice is stored keeps its
                # error. A failure of this fallback too is raised.
                logger.exception("Could not store how turn %s ended; it ends failed", turn_id)
                if not ending.notice_recorded:
                    ending = await self._write_notice(
                        project_id, session_id, turn_id, hub, lock, _UNEXPECTED_FAILURE
                    )
                await self._store_end(project_id, turn_id, hub, ending)
        await self._turn_ended(project_id, session_id, agent, turn_id, cast(TurnEnd, ending.status))

    async def _write_notice(
        self,
        project_id: str,
        session_id: str,
        turn_id: str,
        hub: SessionHub,
        lock: AsyncRLock,
        ending: _Ending,
    ) -> _Ending:
        """Write the ending's failure notice still to be written, if any; return it with none."""
        if ending.failure_notice is None:
            return ending
        await self._executor.record_failure(
            project_id, session_id, turn_id, hub, lock, ending.failure_notice
        )
        return replace(ending, failure_notice=None)

    async def _store_end(
        self, project_id: str, turn_id: str, hub: SessionHub, ending: _Ending
    ) -> None:
        """Store a turn's decided end, other than interrupted, and publish it."""
        await self._store.set_turn(project_id, turn_id, ending.status, error=ending.error)
        await hub.turn_ended(turn_id, status=cast(TurnEnd, ending.status), error=ending.error)

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
    ) -> _Ending:
        """Run the turn task and decide how it ended; `_end_turn` stores it.

        A cancel of the turn task other than by Stop, or of the worker before the turn task
        ended, is raised for an interrupted end. A cancel of the worker that comes once the turn
        task has ended, by itself or by Stop, keeps that end; `_take_turn` raises it after."""
        key = (project_id, session_id)
        if turn_id in self._stopped_turns:
            return _Ending("stopped")
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
                    return _Ending("stopped")
                raise
            if turn_id not in self._stopped_turns:
                raise
            return _Ending("stopped")
        except Exception:
            return _ending_of(turn_id, child_task)
        finally:
            self._child_tasks.pop(key, None)

    async def _turn_started(self, project_id: str, agent: str, turn_id: str) -> None:
        for observer in self._observers:
            await _observed(observer.turn_started(project_id, agent, turn_id))

    async def _turn_ended(
        self, project_id: str, session_id: str, agent: str, turn_id: str, status: TurnEnd
    ) -> None:
        """Every observer hears the end, one after another. It runs inside `_end_turn`'s
        shielded task, so a cancel of the worker waits for the last of them."""
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


def _ending_of(turn_id: str, child_task: asyncio.Task[tuple[TurnEnd, str | None]]) -> _Ending:
    """How a finished, not cancelled turn task ended; an error that escaped it fails the turn."""
    error = child_task.exception()
    if error is not None:
        logger.error("Turn %s failed outside the executor", turn_id, exc_info=error)
        return _UNEXPECTED_FAILURE
    end_status, end_error = child_task.result()
    return _Ending(end_status, end_error)


async def _observed(call: Awaitable[None]) -> None:
    """Await one observer call, bounded. A failing or hanging observer is logged and never
    stops the queue. The end report runs the observers one after another through a cancel,
    so hanging ones hold a shutdown up to OBSERVER_TIMEOUT_S each."""
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
