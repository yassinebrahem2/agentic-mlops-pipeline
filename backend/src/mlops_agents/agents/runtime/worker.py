"""The per-session worker tasks that run a session's queued turns one at a time."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import Literal

from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.contract import MessageView, ToolCallPart, ToolResultPart
from mlops_agents.agents.runtime.executor import TurnExecutor, TurnOutcome
from mlops_agents.agents.runtime.graph import TurnSink
from mlops_agents.agents.runtime.hooks import TurnObserver
from mlops_agents.agents.runtime.hub import AsyncRLock, SessionHub
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.runtime.turn_end import TurnEnds
from mlops_agents.agents.runtime.waiting import _Ending, _ending_of, _observed
from mlops_agents.events.contract import AuditLog, EventLog

logger = logging.getLogger(__name__)

__all__ = ["StopOutcome", "TurnWorkers"]

SessionKey = tuple[str, str]
# What a Stop found on the session's worker (`TurnWorkers.stop`).
StopOutcome = Literal["landed", "dropped", "waiting", "no worker"]


class TurnWorkers:
    """Owns each session's worker task and the turn task it is running."""

    def __init__(
        self,
        *,
        store: SessionStore,
        executor: TurnExecutor,
        observers: Sequence[TurnObserver],
        approval_store: ApprovalStore,
        audit_log: AuditLog | None,
        event_log: EventLog | None,
    ) -> None:
        self._store = store
        self._executor = executor
        self._observers = tuple(observers)
        self._ends = TurnEnds(
            store=store,
            executor=executor,
            observers=self._observers,
            approval_store=approval_store,
            audit_log=audit_log,
            event_log=event_log,
        )
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
        # The workers whose turn's end (or wait) is decided and that have not taken another:
        # from there they only finish their loop, and run no turn (#273).
        self._turn_over: set[SessionKey] = set()
        # The workers in `_turn_over` because their turn went waiting: they only finish their
        # loop (telling the subscribers and observers), and that turn's resume or Stop follows.
        self._went_waiting: set[SessionKey] = set()
        # The waiting turn each resume's worker is about to take, until it takes it or leaves.
        self._resume_turns: dict[SessionKey, str] = {}

    def running(self, key: SessionKey) -> bool:
        worker = self._workers.get(key)
        return worker is not None and not worker.done()

    def get(self, key: SessionKey) -> asyncio.Task[None] | None:
        return self._workers.get(key)

    def running_sessions(self, project_id: str) -> set[str]:
        """Session IDs in project_id whose worker is running a turn, or about to take its first.
        A worker whose turn has ended or waits, and that has not taken another, runs none."""
        return {
            sid
            for (p, sid) in self._workers
            if p == project_id and self.running((p, sid)) and (p, sid) not in self._turn_over
        }

    def start(
        self,
        project_id: str,
        session_id: str,
        agent: str,
        hub: SessionHub,
        lock: AsyncRLock,
        *,
        stops_first_turn: bool = True,
    ) -> None:
        """Start the session's worker. The caller holds the session lock and saw none running.
        A Stop before its first look goes to the turn it takes, as after the Send that started
        it; not for a worker that a Stop starts (`stops_first_turn=False`): a second Stop then
        comes between two turns, and is dropped."""
        key = (project_id, session_id)
        if stops_first_turn:
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
        """Start the session's worker on a waiting turn to resume, then on its queued turns.
        The caller holds the session lock and saw none running."""
        key = (project_id, session_id)
        self._first_look.add(key)
        self._resume_turns[key] = turn_id
        self._workers[key] = asyncio.create_task(
            self._loop(project_id, session_id, agent, hub, lock, resume_turn_id=turn_id)
        )

    def turn_of(self, key: SessionKey) -> str | None:
        """The turn the session's worker is on or, before its first look, about to resume."""
        current = self._current_turns.get(key)
        if current is not None:
            return current
        return self._resume_turns.get(key) if key in self._first_look else None

    def stop(self, key: SessionKey, turn_id: str | None = None) -> StopOutcome:
        """Classify the session's worker for a Stop, and stop its turn when the Stop lands; given
        `turn_id`, only that turn. In one call, with no await, in this order:
        - no worker running: "no worker";
        - its turn went waiting, and it only finishes its loop: "waiting", and nothing is
          cancelled (cancelling the turn task while it reports its wait would orphan its
          checkpoint); the caller stops the waiting turn once the worker is gone;
        - its turn's end is decided, or it is between two turns: "dropped" (stage 1 rule);
        - `turn_id` given and not `turn_of(key)`: "dropped", the worker is left alone;
        - it is on a turn: that turn is stopped (a turn stopped before its task exists ends
          stopped without running): "landed";
        - before its first look: the Stop goes to the turn it takes: "landed";
        - otherwise (a worker that a Stop started, or one going on to the queue after a lost
          take, before it takes a turn): between two turns, "dropped"."""
        if not self.running(key):
            return "no worker"
        if key in self._went_waiting:
            return "waiting"
        if key in self._turn_over:
            return "dropped"
        if turn_id is not None and self.turn_of(key) != turn_id:
            return "dropped"
        current = self._current_turns.get(key)
        if current is not None:
            self._stopped_turns.add(current)
            child = self._child_tasks.get(key)
            if child is not None and not child.done():
                child.cancel()
            return "landed"
        if key in self._first_look:
            self._stop_next.add(key)
            return "landed"
        return "dropped"

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
            self._resume_turns.pop(k, None)
        self._turn_over = {k for k in self._turn_over if project_id not in (None, k[0])}
        self._went_waiting = {k for k in self._went_waiting if project_id not in (None, k[0])}
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
        """Take the turn to resume, if any, then the queued turns, until none is left or a turn
        waits for approval (its resume starts a new worker)."""
        key = (project_id, session_id)
        resuming = resume_turn_id

        try:
            while True:
                async with lock:
                    if resuming is not None:
                        trigger_id = await self._store.turn_trigger_id(project_id, resuming)
                        # The one check that the turn still waits: a Stop that stored it
                        # stopped first wins, and this worker takes nothing (#445).
                        taken = await self._store.take_waiting_turn(project_id, resuming)
                        next_turn: tuple[str, str] | None = (
                            (resuming, trigger_id or "") if taken else None
                        )
                    else:
                        next_turn = await self._store.next_queued_turn(project_id, session_id)
                    if next_turn is None and resuming is not None and self._is_registered(key):
                        # The take lost (the turn was stored stopped first): a Stop meant for
                        # that turn is dropped with it, and the worker goes on to the queued
                        # turns, which a Send that saw it running left to it.
                        self._first_look.discard(key)
                        self._stop_next.discard(key)
                        self._resume_turns.pop(key, None)
                        resuming = None
                        continue
                    if next_turn is None:
                        # Under the lock, so a send from here on starts a new worker.
                        self._leave(key)
                        break
                    # Taking the turn and claiming an early Stop happen with no await between
                    # them, so a Stop lands either here or on the turn itself.
                    self._first_look.discard(key)
                    self._resume_turns.pop(key, None)
                    turn_id, trigger_msg_id = next_turn
                    self._current_turns[key] = turn_id
                    self._turn_over.discard(key)
                    self._went_waiting.discard(key)
                    if key in self._stop_next:
                        self._stop_next.discard(key)
                        self._stopped_turns.add(turn_id)

                resume = resuming is not None
                resuming = None
                try:
                    ended = await self._take_turn(
                        project_id, session_id, agent, turn_id, trigger_msg_id, hub, lock, resume
                    )
                finally:
                    self._current_turns.pop(key, None)
                    self._stopped_turns.discard(turn_id)
                if not ended:
                    break

        finally:
            self._leave(key)

    def _leave(self, key: SessionKey) -> None:
        """Drop the session's worker state, only while the session's worker is this one: once a
        worker is forgotten (a close) its session's state may already be another worker's."""
        if not self._is_registered(key):
            return
        self._first_look.discard(key)
        self._stop_next.discard(key)
        self._turn_over.discard(key)
        self._went_waiting.discard(key)
        self._resume_turns.pop(key, None)
        self._workers.pop(key, None)

    def _is_registered(self, key: SessionKey) -> bool:
        """Whether the calling task is the session's worker."""
        return self._workers.get(key) is asyncio.current_task()

    def _went_waiting_now(self, key: SessionKey) -> None:
        """The turn's wait is stored: the worker no longer runs it, and only finishes its loop."""
        self._turn_over.add(key)
        self._went_waiting.add(key)

    async def _take_turn(
        self,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str,
        trigger_msg_id: str,
        hub: SessionHub,
        lock: AsyncRLock,
        resume: bool,
    ) -> bool:
        """Run or resume a turn and end it; False when it waits for approval instead. A resumed
        turn has been reported started and has read its trigger already."""
        try:
            await self._store.set_turn(project_id, turn_id, "running")
            if not resume:
                await self._turn_started(project_id, agent, turn_id)
                await self._ends.settle_trigger(
                    project_id, session_id, turn_id, trigger_msg_id, hub, lock, read=True
                )
            ending = await self._run_turn(project_id, session_id, agent, turn_id, hub, lock, resume)
        except asyncio.CancelledError:
            # The turn was taken, so it is reported ended, also when the cancel came first.
            self._turn_over.add((project_id, session_id))
            interrupted = _Ending("interrupted")
            await self._ends.end(
                project_id, session_id, agent, turn_id, trigger_msg_id, hub, lock, interrupted
            )
            raise
        # Its end is decided: from here the turn no longer runs, before `turn.ended` is out.
        self._turn_over.add((project_id, session_id))
        if ending.status == "waiting":
            # The turn has not ended: the executor stored the wait and told the subscribers and
            # the observers (`handle_turn_waiting`, the one writer of `waiting`) once
            # `_WaitAwareSink` had taken the session out of the running set, and the
            # turn's resume starts a new worker. Until then the session is busy: no queued turn
            # runs (Decision #281).
            _raise_worker_cancel()
            return False
        # Nothing suspends between deciding the end and ending: a cancel from here on lands in
        # the shielded ending, which keeps the decided status and reports it once.
        await self._ends.end(
            project_id, session_id, agent, turn_id, trigger_msg_id, hub, lock, ending
        )
        _raise_worker_cancel()
        return True

    async def _run_turn(
        self,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str,
        hub: SessionHub,
        lock: AsyncRLock,
        resume: bool,
    ) -> _Ending:
        """Run (or resume) the turn task and decide how it ended; `TurnEnds.end` stores it.

        A cancel of the turn task by Stop ends it stopped, and by anything else but the worker's
        own cancel, interrupted. A cancel of the worker before the turn task ended is raised for
        an interrupted end. A cancel of the worker that comes once the turn task has ended, by
        itself or by Stop, keeps that end; `_take_turn` raises it after."""
        key = (project_id, session_id)
        if turn_id in self._stopped_turns:
            return _Ending("stopped")
        runner = self._executor.resume if resume else self._executor.run
        sink: TurnSink = _WaitAwareSink(hub, lambda: self._went_waiting_now(key))
        child_task = asyncio.create_task(
            runner(project_id, session_id, agent, turn_id, sink, lock=lock)
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
            if turn_id in self._stopped_turns:
                return _Ending("stopped")
            # Neither Stop nor a close: something else cancelled the turn task. The worker is
            # not being cancelled, so it goes on to the next queued turn.
            logger.warning("Turn %s was cancelled by neither Stop nor a close", turn_id)
            return _Ending("interrupted")
        except Exception:
            return _ending_of(turn_id, child_task)
        finally:
            self._child_tasks.pop(key, None)

    async def _turn_started(self, project_id: str, agent: str, turn_id: str) -> None:
        for observer in self._observers:
            await _observed(observer.turn_started(project_id, agent, turn_id))


class _WaitAwareSink:
    """The hub as the turn task's sink. A wait takes the session out of the running set before
    `turn.waiting` reaches any subscriber or observer, so none of them sees it running."""

    def __init__(self, hub: SessionHub, waits: Callable[[], None]) -> None:
        self._hub = hub
        self._waits = waits

    async def text(self, message_id: str, text: str) -> None:
        await self._hub.text(message_id, text)

    async def tool_started(self, message_id: str, call: ToolCallPart) -> None:
        await self._hub.tool_started(message_id, call)

    async def tool_finished(self, message_id: str, result: ToolResultPart) -> None:
        await self._hub.tool_finished(message_id, result)

    async def message(self, message: MessageView, *, created: bool) -> None:
        await self._hub.message(message, created=created)

    async def turn_waiting(self, turn_id: str) -> None:
        self._waits()
        await self._hub.turn_waiting(turn_id)


def _raise_worker_cancel() -> None:
    """Raise the worker's cancel that came in the pass its turn task ended (see `_run_turn`)."""
    current = asyncio.current_task()
    if current is not None and current.cancelling() > 0:
        raise asyncio.CancelledError
