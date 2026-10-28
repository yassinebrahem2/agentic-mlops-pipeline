"""The one owner of an approval decision's records: its audit record and its
`approval.decided` event (Decision #278)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mlops_agents.agents.approvals.settings import get_permission_level
from mlops_agents.agents.approvals.store import ApprovalRecord
from mlops_agents.agents.runtime.contract import DecidedApprovalStatus
from mlops_agents.events.contract import AuditLog, EventLog
from mlops_agents.storage.databases import Databases

__all__ = ["record_decisions"]


async def record_decisions(
    project_id: str,
    approvals: Sequence[ApprovalRecord],
    decision: DecidedApprovalStatus,
    *,
    actor: str,
    databases: Databases,
    audit_log: AuditLog | None,
    event_log: EventLog | None,
    note: str | None = None,
) -> None:
    """Write each decided tool approval's audit record, then each approval's `approval.decided`
    event; a log left out is not written. Only a user's approve or reject carries a note. A
    stall writes no audit record (turn.stalled and turn.resumed record it)."""
    level = (
        await get_permission_level(databases, project_id)
        if audit_log is not None and approvals
        else None
    )
    for approval in approvals:
        if audit_log is not None and level is not None and approval.kind == "tool":
            tool, risk_class = approval.tool, approval.risk_class
            if tool is None or risk_class is None:
                raise ValueError(f"Tool approval {approval.id} has no tool or risk class.")
            detail: dict[str, Any] = {"approval_id": approval.id, "tool": tool}
            if decision in ("approved", "rejected"):
                detail["note"] = note
            await audit_log.record(
                project_id,
                actor=actor,
                action="approval",
                risk_class=risk_class,
                permission_level=level,
                decision=decision,
                target=tool,
                detail=detail,
            )
        if event_log is not None:
            await event_log.record(
                project_id,
                source=actor,
                type="approval.decided",
                subject_id=approval.id,
                payload={"approval_id": approval.id, "status": decision},
            )
