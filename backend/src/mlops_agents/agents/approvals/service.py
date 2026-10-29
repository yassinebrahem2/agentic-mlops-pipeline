"""Approvals service implementation (I12, I13)."""

from __future__ import annotations

from collections.abc import Callable

from mlops_agents.agents.approvals.contract import ApprovalObserver, Approvals, ApprovalView
from mlops_agents.agents.approvals.decisions import settle_decisions
from mlops_agents.agents.approvals.store import ApprovalRecord, ApprovalStore
from mlops_agents.agents.runtime.contract import AgentSessions, Part
from mlops_agents.agents.tools.base import Preview
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import AuditLog, EventLog
from mlops_agents.storage.databases import Databases


class ApprovalsService(Approvals):
    """Enforces approval decisions, updates message parts, records audit/events, and resumes
    or stops turns. Its store tells the observers, before any turn moves on."""

    def __init__(
        self,
        databases: Databases,
        sessions: AgentSessions,
        *,
        store: ApprovalStore,
        audit_log: AuditLog | None = None,
        event_log: EventLog | None = None,
    ) -> None:
        self._databases = databases
        self._sessions = sessions
        self._store = store
        self._audit_log = audit_log
        self._event_log = event_log

    def add_observer(self, observer: ApprovalObserver) -> None:
        self._store.add_observer(observer)

    async def pending(self, project_id: str) -> list[ApprovalView]:
        """List all pending approvals for a project."""
        await self._sessions.ensure_recovered(project_id)
        records = await self._store.pending(project_id)
        return [ApprovalView.model_validate(r) for r in records]

    async def create_stall(
        self, project_id: str, turn_id: str, step: int, preview: Preview
    ) -> ApprovalView:
        """The pending stall of the turn's step; only the call that creates it records
        `approval.requested`."""
        record, created = await self._store.create_stall(project_id, turn_id, step, preview)
        if created and self._event_log is not None:
            await self._event_log.record(
                project_id,
                source=record.agent,
                type="approval.requested",
                subject_id=record.id,
                payload={"approval_id": record.id, "turn_id": turn_id, "tool": None},
            )
        return ApprovalView.model_validate(record)

    async def decide(
        self,
        project_id: str,
        approval_id: str,
        *,
        approve: bool,
        note: str | None = None,
    ) -> ApprovalView:
        """Decide an approval request (approve or reject), update parts, audit, and resume if
        done. Rejecting a stall stops its turn instead."""
        await self._sessions.ensure_recovered(project_id)
        record = await self._pending_record(project_id, approval_id)

        # Atomic update in store; the approval keeps the task it was asked under.
        updated_rec = await self._store.decide(project_id, approval_id, approve=approve, note=note)
        await self._settle(project_id, updated_rec)

        if record.kind == "stall" and not approve:
            await self._sessions.stop(project_id, record.agent)
        else:
            await self._resume_when_none_pending(project_id, record)
        return ApprovalView.model_validate(updated_rec)

    async def cancel(self, project_id: str, approval_id: str) -> None:
        """Cancel a pending approval (a superseded proposal); resumes its turn when none of
        the turn's approvals is still pending."""
        await self._sessions.ensure_recovered(project_id)
        record = await self._pending_record(project_id, approval_id)
        cancelled = await self._store.cancel(project_id, approval_id)
        await self._settle(project_id, cancelled)
        await self._resume_when_none_pending(project_id, record)

    async def _pending_record(self, project_id: str, approval_id: str) -> ApprovalRecord:
        record = await self._store.get(project_id, approval_id)
        if record is None:
            raise DomainError(
                "approval_not_found",
                f"Approval '{approval_id}' does not exist.",
                status=404,
            )
        if record.status != "pending":
            raise DomainError(
                "approval_not_pending",
                f"Approval '{approval_id}' is not pending (status: {record.status}).",
                status=409,
            )
        return record

    async def _settle(self, project_id: str, record: ApprovalRecord) -> None:
        """Write the decision's records and its card, the card through the live session."""

        async def rewrite_part(
            session_id: str, message_id: str, transform: Callable[[list[Part]], list[Part]]
        ) -> object:
            return await self._sessions.update_message(
                project_id, session_id, message_id, transform
            )

        await settle_decisions(
            project_id,
            [record],
            store=self._store,
            databases=self._databases,
            audit_log=self._audit_log,
            event_log=self._event_log,
            rewrite_part=rewrite_part,
        )

    async def _resume_when_none_pending(self, project_id: str, record: ApprovalRecord) -> None:
        """Resume the turn once none of its approvals is pending or still settling. A resumed turn
        reads its message once and writes it back, so a card written after the resume is lost:
        the last decision to settle resumes it (each one settles before it looks)."""
        step_approvals = await self._store.list_for_step(project_id, record.turn_id)
        if not any(a.status == "pending" or a.settled_at is None for a in step_approvals):
            await self._sessions.resume_turn(project_id, record.session_id, record.turn_id)
