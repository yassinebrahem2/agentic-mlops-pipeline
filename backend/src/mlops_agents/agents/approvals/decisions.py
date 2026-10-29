"""The one owner of a decided approval's records and card: its audit record and its
`approval.decided` event (Decision #278), the card in its message, and `settled_at` (#381)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from mlops_agents.agents.approvals.settings import get_permission_level
from mlops_agents.agents.approvals.store import ApprovalRecord, ApprovalStore
from mlops_agents.agents.runtime.contract import ApprovalPart, DecidedApprovalStatus, Part
from mlops_agents.events.contract import AuditLog, EventLog
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import events

logger = logging.getLogger(__name__)

__all__ = ["PartRewriter", "record_decisions", "settle_decisions"]

# Rewrites one message's parts: (session ID, message ID, transform); each caller binds its
# project, and stores (and publishes, when live) the result. None: the message is gone.
PartRewriter = Callable[[str, str, Callable[[list[Part]], list[Part]]], Awaitable[object]]

# Literal text, as the partial expression index audit_approval_id (0006) is written: SQLite
# matches a query to it only by these terms, so neither the path nor 'approval' may be bound.
_HAS_AUDIT = text(
    "SELECT 1 FROM audit WHERE action = 'approval' "
    "AND json_extract(detail, '$.approval_id') = :approval_id LIMIT 1"
)


async def _has_audit(engine: AsyncEngine, approval_id: str) -> bool:
    async with engine.connect() as conn:
        found = await conn.execute(_HAS_AUDIT, {"approval_id": approval_id})
        return found.first() is not None


async def _has_event(engine: AsyncEngine, approval_id: str) -> bool:
    query = (
        select(events.c.seq)
        .where(events.c.type == "approval.decided", events.c.subject_id == approval_id)
        .limit(1)
    )
    async with engine.connect() as conn:
        return (await conn.execute(query)).first() is not None


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
    stall writes no audit record (turn.stalled and turn.resumed record it). Idempotent per
    approval: a record it finds is not written again, so a call after a crash between the two
    writes only the missing one. Two calls at once are serialized by `settle_decisions`."""
    level = (
        await get_permission_level(databases, project_id)
        if audit_log is not None and approvals
        else None
    )
    engine = await databases.project(project_id)
    for approval in approvals:
        if (
            audit_log is not None
            and level is not None
            and approval.kind == "tool"
            and not await _has_audit(engine, approval.id)
        ):
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
        if event_log is not None and not await _has_event(engine, approval.id):
            await event_log.record(
                project_id,
                source=actor,
                type="approval.decided",
                subject_id=approval.id,
                payload={"approval_id": approval.id, "status": decision},
            )


async def settle_decisions(
    project_id: str,
    approvals: Sequence[ApprovalRecord],
    *,
    store: ApprovalStore,
    databases: Databases,
    audit_log: AuditLog | None,
    event_log: EventLog | None,
    rewrite_part: PartRewriter,
) -> None:
    """Write each decided approval's records, then its card (one write per message), then mark
    it settled: an approval is settled only once both are written, so a call again after a
    crash finishes it. The records' actor is the approval's `decided_by` (unknown is "system").
    The settle lock covers only the records: `rewrite_part` may take the session lock, which
    Stop holds while it settles."""
    decided: list[tuple[ApprovalRecord, DecidedApprovalStatus]] = []
    for approval in approvals:
        status = approval.status
        if status == "pending":
            raise ValueError(f"Approval {approval.id} is not decided.")
        decided.append((approval, status))
    if not decided:
        return

    async with store.settling(project_id):
        for approval, status in decided:
            await record_decisions(
                project_id,
                [approval],
                status,
                actor=approval.decided_by or "system",
                databases=databases,
                audit_log=audit_log,
                event_log=event_log,
                note=approval.note,
            )

    by_message: dict[tuple[str, str], dict[str, ApprovalRecord]] = {}
    for approval, _ in decided:
        by_message.setdefault((approval.session_id, approval.message_id), {})[approval.id] = (
            approval
        )
    for (session_id, message_id), cards in by_message.items():
        if await rewrite_part(session_id, message_id, _cards_of(cards)) is None:
            # Nothing holds the card any more; the decision's records are written all the same.
            logger.warning(
                "Message %s is gone: approvals %s are settled without their card.",
                message_id,
                ", ".join(cards),
            )

    await store.mark_settled(project_id, [approval.id for approval, _ in decided])


def _cards_of(cards: dict[str, ApprovalRecord]) -> Callable[[list[Part]], list[Part]]:
    """A transform that sets each listed approval's card to its record's status and note."""

    def transform(parts: list[Part]) -> list[Part]:
        return [
            part.model_copy(
                update={
                    "status": cards[part.approval_id].status,
                    "note": cards[part.approval_id].note,
                }
            )
            if isinstance(part, ApprovalPart) and part.approval_id in cards
            else part
            for part in parts
        ]

    return transform
