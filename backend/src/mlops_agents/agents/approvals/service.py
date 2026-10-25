"""Approvals service implementation (I12)."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any, Protocol

from mlops_agents.agents.approvals.contract import Approvals, ApprovalView
from mlops_agents.agents.approvals.settings import get_permission_level
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.contract import ApprovalPart, MessageView, Part
from mlops_agents.agents.tasks.contract import TaskWorkspaces
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import AuditLog, EventLog
from mlops_agents.storage.databases import Databases


class ResumableSessions(Protocol):
    async def update_message(
        self,
        project_id: str,
        session_id: str,
        message_id: str,
        transform: Callable[[list[Part]], list[Part]],
    ) -> MessageView: ...

    async def resume_turn(self, project_id: str, session_id: str, turn_id: str) -> None: ...


class ApprovalsService(Approvals):
    """Enforces approval decisions, updates message parts, records audit/events, and resumes turns."""

    def __init__(
        self,
        databases: Databases,
        sessions: Any,
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

        # Task that was current when decided
        current_task = (
            await self._workspaces.current(project_id, record.session_id)
            if self._workspaces is not None
            else None
        )
        task_id = current_task.id if current_task is not None else None

        # Update in store
        status_val = "approved" if approve else "rejected"
        updated_rec = await self._store.decide(
            project_id,
            approval_id,
            approve=approve,
            note=note,
            task_id=task_id,
        )

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

        with contextlib.suppress(Exception):
            await self._sessions.update_message(
                project_id, record.session_id, record.message_id, _update_part
            )

        # Audit log entry
        if self._audit_log is not None:
            try:
                level = await get_permission_level(self._databases, project_id)
            except Exception:
                level = "ask"

            await self._audit_log.record(
                project_id,
                actor="user",
                action="approval",
                risk_class=record.risk_class,
                permission_level=level,
                decision=status_val,
                target=record.tool,
                detail={"approval_id": approval_id, "tool": record.tool, "note": note},
            )

        # Event log entry
        if self._event_log is not None:
            await self._event_log.record(
                project_id,
                source="user",
                type="approval.decided",
                subject_id=approval_id,
                payload={
                    "approval_id": approval_id,
                    "status": status_val,
                },
            )

        # Check remaining pending approvals for this turn
        step_approvals = await self._store.list_for_step(project_id, record.turn_id)
        if not any(a.status == "pending" for a in step_approvals) and hasattr(
            self._sessions, "resume_turn"
        ):
            await self._sessions.resume_turn(project_id, record.session_id, record.turn_id)

        return ApprovalView.model_validate(updated_rec)
