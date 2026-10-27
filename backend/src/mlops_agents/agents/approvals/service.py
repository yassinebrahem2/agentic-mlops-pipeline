"""Approvals service implementation (I12)."""

from __future__ import annotations

from mlops_agents.agents.approvals.contract import Approvals, ApprovalView
from mlops_agents.agents.approvals.decisions import record_decisions
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.contract import AgentSessions, ApprovalPart, Part
from mlops_agents.agents.tasks.contract import TaskWorkspaces
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import AuditLog, EventLog
from mlops_agents.storage.databases import Databases


class ApprovalsService(Approvals):
    """Enforces approval decisions, updates message parts, records audit/events, and resumes turns."""

    def __init__(
        self,
        databases: Databases,
        sessions: AgentSessions,
        *,
        store: ApprovalStore | None = None,
        audit_log: AuditLog | None = None,
        event_log: EventLog | None = None,
        workspaces: TaskWorkspaces | None = None,
    ) -> None:
        self._databases = databases
        self._sessions = sessions
        self._store = store if store is not None else ApprovalStore(databases)
        self._audit_log = audit_log
        self._event_log = event_log
        self._workspaces = workspaces

    async def pending(self, project_id: str) -> list[ApprovalView]:
        """List all pending approvals for a project."""
        await self._sessions.ensure_recovered(project_id)
        records = await self._store.pending(project_id)
        return [ApprovalView.model_validate(r) for r in records]

    async def decide(
        self,
        project_id: str,
        approval_id: str,
        *,
        approve: bool,
        note: str | None = None,
    ) -> ApprovalView:
        """Decide an approval request (approve or reject), update parts, audit, and resume if done."""
        await self._sessions.ensure_recovered(project_id)

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

        # Atomic update in store; the approval keeps the task it was asked under.
        status_val = "approved" if approve else "rejected"
        updated_rec = await self._store.decide(project_id, approval_id, approve=approve, note=note)

        # Update message part in session
        def _update_part(parts: list[Part]) -> list[Part]:
            res: list[Part] = []
            for part in parts:
                if isinstance(part, ApprovalPart) and part.approval_id == approval_id:
                    res.append(
                        ApprovalPart(
                            type="approval",
                            approval_id=part.approval_id,
                            tool_call_id=part.tool_call_id,
                            preview=part.preview,
                            status=status_val,
                            note=note,
                        )
                    )
                else:
                    res.append(part)
            return res

        await self._sessions.update_message(
            project_id, record.session_id, record.message_id, _update_part
        )

        await record_decisions(
            project_id,
            [updated_rec],
            status_val,
            actor="user",
            databases=self._databases,
            audit_log=self._audit_log,
            event_log=self._event_log,
            note=note,
        )

        # Check remaining pending approvals for this turn
        step_approvals = await self._store.list_for_step(project_id, record.turn_id)
        if not any(a.status == "pending" for a in step_approvals):
            await self._sessions.resume_turn(project_id, record.session_id, record.turn_id)

        return ApprovalView.model_validate(updated_rec)
