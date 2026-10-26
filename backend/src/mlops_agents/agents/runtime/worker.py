"""The per-session worker tasks that run a session's queued turns one at a time."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, replace
from typing import Literal, cast

from mlops_agents.agents.runtime.contract import TextPart, TurnEnd
from mlops_agents.agents.runtime.executor import (
    UNEXPECTED_FAILURE_TEXT,
    TurnExecutor,
    TurnOutcome,
)
from mlops_agents.agents.runtime.hooks import TurnObserver
from mlops_agents.agents.runtime.hub import AsyncRLock, SessionHub
from mlops_agents.agents.runtime.shielded import shielded
from mlops_agents.agents.runtime.store import SessionStore

logger = logging.getLogger(__name__)

__all__ = ["TurnWorkers"]

SessionKey = tuple[str, str]

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
        self._child_tasks: dict[SessionKey, asyncio.Task[TurnOutcome]] = {}
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

    def get(self, key: SessionKey) -> asyncio.Task[None] | None:
        return self._workers.get(key)

    def running_sessions(self, project_id: str) -> set[str]:
        """Session IDs in project_id with a currently running worker."""
        return {sid for (p, sid) in self._workers if p == project_id and self.running((p, sid))}

    def start(
        self, project_id: str, session_id: str, agent: str, hub: SessionHub, lock: AsyncRLock
    ) -> None:
        """Start the session's worker. The caller holds the session lock and saw none running."""
        key = (project_id, session_id)
        self._first_look.add(key)
        self._workers[key] = asyncio.create_task(
            self._loop(project_id, session_id, agent, hub, lock)
        )

    def resume(
        self,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str,
        hub: SessionHub,
        lock: AsyncRLock,
    ) -> None:
        """Resume a waiting turn in a worker."""
        key = (project_id, session_id)
        self._first_look.add(key)
        self._workers[key] = asyncio.create_task(
            self._loop(project_id, session_id, agent, hub, lock, resume_turn_id=turn_id)
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
        keys = [k for k in self._workers if project_id in (None, k[0])]
        for k in keys:
            self._workers.pop(k, None)
            self._child_tasks.pop(k, None)
            self._current_turns.pop(k, None)
        self._first_look = {k for k in self._first_look if project_id not in (None, k[0])}
        self._stop_next = {k for k in self._stop_next if project_id not in (None, k[0])}
        if project_id is None:
            self._stopped_turns.clear()

    async def _loop(
        self,
        project_id: str,
        session_id: str,
        agent: str,
        hub: SessionHub,
        lock: AsyncRLock,
        resume_turn_id: str | None = None,
    ) -> None:
        key = (project_id, session_id)

        try:
            if resume_turn_id is not None:
                async with lock:
                    self._first_look.discard(key)
                    turn_id = resume_turn_id
                    self._current_turns[key] = turn_id
                    if key in self._stop_next:
                        self._stop_next.discard(key)
                        self._stopped_turns.add(turn_id)

                try:
                    trigger_id = await self._store.turn_trigger_id(project_id, turn_id) or ""
                    should_continue = await self._execute_turn(
                        project_id, session_id, agent, turn_id, trigger_id, hub, lock, resume=True
                    )
                    if not should_continue:
                        return
                finally:
                    self._current_turns.pop(key, None)
                    self._stopped_turns.discard(turn_id)

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
                    should_continue = await self._execute_turn(
                        project_id,
                        session_id,
                        agent,
                        turn_id,
                        trigger_msg_id,
                        hub,
                        lock,
                        resume=False,
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

    async def _execute_turn(
        self,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str,
        trigger_msg_id: str,
        hub: SessionHub,
        lock: AsyncRLock,
        *,
        resume: bool = False,
    ) -> bool:
        """Run or resume a turn and record its ended/waiting state."""
        try:
            await self._store.set_turn(project_id, turn_id, "running")
            if not resume:
                await self._turn_started(project_id, agent, turn_id)
                await self._settle_trigger(
                    project_id, session_id, turn_id, trigger_msg_id, hub, lock, read=True
                )
            ending = await self._run_turn_task(
                project_id, session_id, agent, turn_id, hub, lock, resume=resume
            )
        except asyncio.CancelledError:
            interrupted = _Ending("interrupted")
            await self._end_turn(
                project_id, session_id, agent, turn_id, trigger_msg_id, hub, lock, interrupted
            )
            raise

        if ending.status == "waiting":
            await self._store.set_turn(project_id, turn_id, "waiting")
            return False

        await self._end_turn(
            project_id, session_id, agent, turn_id, trigger_msg_id, hub, lock, ending
        )
        current = asyncio.current_task()
        if current is not None and current.cancelling() > 0:
            raise asyncio.CancelledError
        return True

    async def _run_turn_task(
        self,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str,
        hub: SessionHub,
        lock: AsyncRLock,
        *,
        resume: bool = False,
    ) -> _Ending:
        key = (project_id, session_id)
        if turn_id in self._stopped_turns:
            return _Ending("stopped")
        runner = self._executor.resume if resume else self._executor.run
        child_task = asyncio.create_task(
            runner(project_id, session_id, agent, turn_id, hub, lock=lock)
        )
        self._child_tasks[key] = child_task
        try:
            await child_task
            return _ending_of(turn_id, child_task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling() > 0:
                if child_task.cancelling() == 0 and not child_task.cancelled():
                    return _ending_of(turn_id, child_task)
                if child_task.cancelled() and turn_id in self._stopped_turns:
                    return _Ending("stopped")
                raise
            if turn_id not in self._stopped_turns:
                raise
            return _Ending("stopped")
        except Exception:
            return _ending_of(turn_id, child_task)
        finally:
            self._child_tasks.pop(key, None)

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
        """The one owner of a taken turn's end: its stored end state, then the report to every observer."""
        if ending.status == "waiting":
            return

        if ending.status == "interrupted":
            await self._store.set_turn(project_id, turn_id, "interrupted")
            await self._store.mark_interrupted(project_id, turn_id)
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
        if ending.failure_notice is None:
            return ending
        await self._executor.record_failure(
            project_id, session_id, turn_id, hub, lock, ending.failure_notice
        )
        return replace(ending, failure_notice=None)

    async def _store_end(
        self, project_id: str, turn_id: str, hub: SessionHub, ending: _Ending
    ) -> None:
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
            logger.exception("Could not read the final text of turn %s", turn_id)
            final_text = None
        for observer in self._observers:
            await _observed(observer.turn_ended(project_id, agent, turn_id, status, final_text))

    async def _final_text(self, project_id: str, session_id: str, turn_id: str) -> str | None:
        message = await self._store.turn_agent_message(project_id, session_id, turn_id)
        if message is None:
            return None
        texts = [part.text for part in message.parts if isinstance(part, TextPart) and part.text]
        return texts[-1] if texts else None


def _ending_of(turn_id: str, child_task: asyncio.Task[TurnOutcome]) -> _Ending:
    error = child_task.exception()
    if error is not None:
        logger.error("Turn %s failed outside the executor", turn_id, exc_info=error)
        return _UNEXPECTED_FAILURE
    end_status, end_error = child_task.result()
    return _Ending(end_status, end_error)


async def _observed(call: Awaitable[None]) -> None:
    """Await one observer call, bounded. A failing or hanging observer is logged and never
    stops the queue."""
    try:
        async with asyncio.timeout(OBSERVER_TIMEOUT_S):
            await call
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling() > 0:
            raise
        logger.exception("A turn observer was cancelled")
    except Exception:
        logger.exception("A turn observer failed")
