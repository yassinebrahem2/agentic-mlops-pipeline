"""A project's recovery at its first use after a start, under that project's own lock (#380 item
3), and the repair of the decisions a crash cut after their status commit (#381): told to the
observers again, settled, and their waiting turns finished."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence

from mlops_agents.agents.approvals.decisions import settle_decisions
from mlops_agents.agents.approvals.store import ApprovalRecord, ApprovalStore
from mlops_agents.agents.runtime.contract import Part
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.events.contract import AuditLog, EventLog
from mlops_agents.storage.databases import Databases

logger = logging.getLogger(__name__)

__all__ = ["ProjectRecovery", "repair_unsettled_approvals"]

# (project ID, session ID, message ID, transform): stores and publishes the message's new parts,
# under the session lock; None when the message is gone.
ProjectPartRewriter = Callable[
    [str, str, str, Callable[[list[Part]], list[Part]]], Awaitable[object]
]
# (project ID, session ID, turn ID): ends or resumes the turn when it waits; else does nothing.
TurnFinisher = Callable[[str, str, str], Awaitable[None]]


async def repair_unsettled_approvals(
    project_id: str,
    *,
    unsettled: Sequence[ApprovalRecord],
    approval_store: ApprovalStore,
    store: SessionStore,
    databases: Databases,
    audit_log: AuditLog | None,
    event_log: EventLog | None,
    rewrite_part: ProjectPartRewriter,
    stop_turn: TurnFinisher,
    resume_turn: TurnFinisher,
) -> None:
    """Finish what a crash cut, in the live path's order: the observers, the records and card,
    then the turn. `unsettled` is the snapshot taken inside the project's recovery lock, so a
    decision made after it is a live one, told and settled by its own path. The turns are
    finished through the session manager, which owns their locks and workers."""
    if unsettled:
        # The status was committed, so the observers may not have finished (a proposal still
        # pending is applied once by its observer); observers are idempotent.
        await approval_store.retell_decided(unsettled)
        await settle_decisions(
            project_id,
            unsettled,
            store=approval_store,
            databases=databases,
            audit_log=audit_log,
            event_log=event_log,
            rewrite_part=lambda session_id, message_id, transform: rewrite_part(
                project_id, session_id, message_id, transform
            ),
        )

    # A turn waits only for its approvals, so one with none pending is finished: a rejected
    # stall ends it, anything else resumes it. One with an approval not settled is left to the
    # decision that will settle it (a live one still in its observers), or to the next start.
    turns: dict[tuple[str, str], list[ApprovalRecord]] = {}
    for approval in await approval_store.of_waiting_turns(project_id):
        turns.setdefault((approval.session_id, approval.turn_id), []).append(approval)
    for (session_id, turn_id), approvals in turns.items():
        if any(a.status == "pending" or a.settled_at is None for a in approvals):
            continue
        stalled = any(a.kind == "stall" and a.status == "rejected" for a in approvals)
        finish = stop_turn if stalled else resume_turn
        try:
            await finish(project_id, session_id, turn_id)
        except Exception:
            logger.exception("Could not finish turn %s after the repair; it stays waiting", turn_id)


class ProjectRecovery:
    """Owns which projects are recovered, one recovery lock per project, and each project's
    repair task. A project is recovered at its first use; close-time recovery never repairs."""

    def __init__(
        self,
        *,
        store: SessionStore,
        approval_store: ApprovalStore,
        databases: Databases,
        audit_log: AuditLog | None,
        event_log: EventLog | None,
        rewrite_part: ProjectPartRewriter,
        stop_turn: TurnFinisher,
        resume_turn: TurnFinisher,
    ) -> None:
        self._store = store
        self._approval_store = approval_store
        self._databases = databases
        self._audit_log = audit_log
        self._event_log = event_log
        self._rewrite_part = rewrite_part
        self._stop_turn = stop_turn
        self._resume_turn = resume_turn
        self._recovered: set[str] = set()
        # Dropped only all at once (close): dropping one a caller still holds would let two
        # recoveries of that project run at once.
        self._locks: dict[str, asyncio.Lock] = {}
        self._repairs: dict[str, asyncio.Task[None]] = {}

    def recovered(self) -> set[str]:
        return set(self._recovered)

    async def ensure(self, project_id: str) -> None:
        """Recover the project unless it is already: its running turns and expiry, then the
        repair. The first caller waits for the repair, and a cancel of it cancels only its wait;
        later callers do not wait for it, so the repair's observers can call back in here."""
        async with self._locks.setdefault(project_id, asyncio.Lock()):
            if project_id in self._recovered:
                return
            await self._store.recover(
                project_id,
                approval_store=self._approval_store,
                audit_log=self._audit_log,
                event_log=self._event_log,
            )
            snapshot = await self._approval_store.unsettled(project_id)
            self._recovered.add(project_id)
            # Started inside the lock only so that no second caller starts another.
            repair = asyncio.create_task(self._repair(project_id, snapshot))
            self._repairs[project_id] = repair
        await asyncio.shield(repair)

    async def _repair(self, project_id: str, snapshot: list[ApprovalRecord]) -> None:
        try:
            await repair_unsettled_approvals(
                project_id,
                unsettled=snapshot,
                approval_store=self._approval_store,
                store=self._store,
                databases=self._databases,
                audit_log=self._audit_log,
                event_log=self._event_log,
                rewrite_part=self._rewrite_part,
                stop_turn=self._stop_turn,
                resume_turn=self._resume_turn,
            )
        except Exception:
            # Every step is idempotent: the next start repairs what this one could not.
            logger.exception("The repair of project %s failed", project_id)

    async def recover_only(self, project_id: str) -> None:
        """Close-time recovery: ends running turns and expires; never tells, settles or resumes."""
        try:
            await self._store.recover(
                project_id,
                approval_store=self._approval_store,
                audit_log=self._audit_log,
                event_log=self._event_log,
            )
        except Exception:
            logger.exception("Failed to recover project %s", project_id)

    async def cancel(self, project_id: str | None = None) -> None:
        """Cancel the repairs of one project, or of all, and wait until they ended; a repair cut
        here is done again at the next start."""
        repairs = [t for p, t in self._repairs.items() if project_id in (None, p)]
        for repair in repairs:
            repair.cancel()
        if repairs:
            await asyncio.gather(*repairs, return_exceptions=True)

    def forget(self, project_id: str | None = None) -> None:
        """Forget that one project, or every project, was recovered."""
        if project_id is None:
            self._recovered.clear()
            self._locks.clear()
            self._repairs.clear()
            return
        self._recovered.discard(project_id)
        self._repairs.pop(project_id, None)
