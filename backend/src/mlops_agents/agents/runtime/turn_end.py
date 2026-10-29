"""A taken turn's end: its stored end state, then the report to every turn observer."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.contract import MessageStatus, TextPart, TurnEnd
from mlops_agents.agents.runtime.executor import TurnExecutor
from mlops_agents.agents.runtime.hooks import TurnObserver
from mlops_agents.agents.runtime.hub import AsyncRLock, SessionHub
from mlops_agents.agents.runtime.shielded import shielded
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.runtime.waiting import (
    _Ending,
    _observed,
    cancel_turn_approvals,
    finish_waiting_message,
)
from mlops_agents.events.contract import AuditLog, EventLog

logger = logging.getLogger(__name__)

__all__ = ["TurnEnds"]

# The final status of an agent message still `waiting` when its turn ends this way. A done turn
# completed its message as it ended; an interrupted one is recovery's.
_WAITING_MESSAGE_ENDS: dict[TurnEnd, MessageStatus] = {"stopped": "stopped", "failed": "complete"}


class TurnEnds:
    """The one owner of a taken turn's end, and of its trigger message's settling."""

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
        self._approval_store = approval_store
        self._audit_log = audit_log
        self._event_log = event_log

    @shielded
    async def end(
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
        """Store a taken turn's end state, then report it to every observer. It runs whole: a
        cancel of the worker (close, close_project, or both) waits for it and is raised after,
        so it never rewrites the end nor reports it twice.

        It runs in its own task and takes the session lock, which the worker never holds when
        it gets here, and the turn task has finished by then.

        A turn waiting for approval has not ended: there is nothing to store or report. Every
        other end but interrupted (recovery expires those) cancels the approvals its turn left
        pending, once the end is stored: the turn task has finished, so none comes after it.
        """
        status = ending.status
        if status == "waiting":
            return
        if status == "interrupted":
            await self._end_interrupted(project_id, session_id, turn_id, trigger_msg_id, hub, lock)
        else:
            try:
                ending = await self._write_notice(
                    project_id, session_id, turn_id, hub, lock, ending
                )
                await self._store_end(project_id, turn_id, hub, status, ending.error)
            except Exception:
                # As "disk full": the end is still recorded once more, with the writes it needs,
                # and reported. It keeps its own status.
                logger.exception("Could not store how turn %s ended; it ends %s", turn_id, status)
                await self._record_end(project_id, session_id, turn_id, hub, lock, status, ending)
            await self._close_out(project_id, session_id, turn_id, hub, lock, status)
        await self._turn_ended(project_id, session_id, agent, turn_id, status)

    async def _close_out(
        self,
        project_id: str,
        session_id: str,
        turn_id: str,
        hub: SessionHub,
        lock: AsyncRLock,
        status: TurnEnd,
    ) -> None:
        """Cancel the ended turn's pending approvals (by the user for a Stop, else by the system),
        then give a still-waiting agent message its final status. Neither raises: a sweep that
        fails leaves its approvals decided and unsettled (the next start's repair settles them)
        or pending (the next recovery expires them)."""
        try:
            await cancel_turn_approvals(
                project_id,
                session_id,
                turn_id,
                decided_by="user" if status == "stopped" else "system",
                store=self._store,
                hub=hub,
                lock=lock,
                approval_store=self._approval_store,
                audit_log=self._audit_log,
                event_log=self._event_log,
            )
        except Exception:
            logger.exception("Could not cancel the pending approvals of turn %s", turn_id)
        final = _WAITING_MESSAGE_ENDS.get(status)
        if final is None:
            return
        try:
            await finish_waiting_message(
                project_id, session_id, turn_id, final, store=self._store, hub=hub, lock=lock
            )
        except Exception:
            logger.exception("Could not give turn %s's waiting message its end", turn_id)

    async def settle_trigger(
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

    async def _end_interrupted(
        self,
        project_id: str,
        session_id: str,
        turn_id: str,
        trigger_msg_id: str,
        hub: SessionHub,
        lock: AsyncRLock,
    ) -> None:
        """Store an interrupted end and publish it. It never raises: a close is about to raise
        its cancel, or the worker goes on to the next queued turn."""
        try:
            await self._store.set_turn(project_id, turn_id, "interrupted")
            await self._store.mark_interrupted(project_id, turn_id)
            # A cancel before the trigger was read would leave it queued for good.
            await self.settle_trigger(
                project_id, session_id, turn_id, trigger_msg_id, hub, lock, read=False
            )
            await hub.turn_ended(turn_id, status="interrupted")
        except Exception:
            # As "database is locked" while close_project runs: the end is still recorded once
            # more, with the one write it needs, and reported.
            logger.exception("Could not store how turn %s ended; it ends interrupted", turn_id)
            interrupted = _Ending("interrupted")
            await self._record_end(
                project_id, session_id, turn_id, hub, lock, "interrupted", interrupted
            )

    async def _record_end(
        self,
        project_id: str,
        session_id: str,
        turn_id: str,
        hub: SessionHub,
        lock: AsyncRLock,
        status: TurnEnd,
        ending: _Ending,
    ) -> None:
        """The fallback of every end: the failure notice still to be written, the status, then
        its publish, each logged when it fails, so it never raises: a close is about to raise
        its cancel, or the worker goes on to the next queued turn. A turn has one failure notice
        (`record_failure` writes no second one). A turn whose status cannot be written stays
        running in the database; the next recovery (at close or at startup) marks it
        interrupted."""
        try:
            ending = await self._write_notice(project_id, session_id, turn_id, hub, lock, ending)
        except Exception:
            logger.exception("Could not write the failure notice of turn %s", turn_id)
        try:
            await self._store.set_turn(project_id, turn_id, status, error=ending.error)
        except Exception:
            logger.exception("Could not record turn %s %s", turn_id, status)
        try:
            await hub.turn_ended(turn_id, status=status, error=ending.error)
        except Exception:
            logger.exception("Could not publish the end of turn %s", turn_id)

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
        self, project_id: str, turn_id: str, hub: SessionHub, status: TurnEnd, error: str | None
    ) -> None:
        """Store a turn's decided end, other than interrupted, and publish it."""
        await self._store.set_turn(project_id, turn_id, status, error=error)
        await hub.turn_ended(turn_id, status=status, error=error)

    async def _turn_ended(
        self, project_id: str, session_id: str, agent: str, turn_id: str, status: TurnEnd
    ) -> None:
        """Every observer hears the end, one after another. It runs inside `end`'s shielded
        task, so a cancel of the worker waits for the last of them."""
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
