"""Approval storage backed by project.db (I12)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.sqlite import insert

from mlops_agents.agents.approvals.contract import ApprovalView
from mlops_agents.agents.runtime.contract import ApprovalStatus
from mlops_agents.agents.tools.base import Preview, RiskClass
from mlops_agents.clock import now_iso
from mlops_agents.errors import DomainError
from mlops_agents.ids import new_id
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import approvals, turns

_UNSET = object()


class ApprovalRecord(ApprovalView):
    """Database representation of an approval request, including its message_id."""

    message_id: str
    task_id: str | None = None


def _row_to_record(row: Any) -> ApprovalRecord:
    preview_raw = row["preview"]
    if isinstance(preview_raw, str):
        preview_obj = Preview.model_validate_json(preview_raw)
    elif isinstance(preview_raw, dict):
        preview_obj = Preview.model_validate(preview_raw)
    else:
        preview_obj = cast(Preview, preview_raw)

    risk_class_raw = row["risk_class"]
    risk_class_obj = (
        RiskClass(risk_class_raw) if not isinstance(risk_class_raw, RiskClass) else risk_class_raw
    )
    task_id = str(row["task_id"]) if row["task_id"] is not None else None

    return ApprovalRecord(
        id=str(row["id"]),
        turn_id=str(row["turn_id"]),
        session_id=str(row["session_id"]),
        message_id=str(row["message_id"]),
        agent=str(row["agent"]),
        tool_call_id=str(row["tool_call_id"]),
        tool=str(row["tool"]),
        risk_class=risk_class_obj,
        preview=preview_obj,
        status=cast(ApprovalStatus, row["status"]),
        note=row["note"],
        created_at=str(row["created_at"]),
        decided_at=row["decided_at"],
        task_id=task_id,
    )


class ApprovalStore:
    """Store for approval requests in project.db."""

    def __init__(self, databases: Databases) -> None:
        self._databases = databases

    async def create(
        self,
        project_id: str,
        *,
        turn_id: str,
        session_id: str,
        message_id: str,
        agent: str,
        tool_call_id: str,
        tool: str,
        risk_class: RiskClass | str,
        preview: Preview,
        status: ApprovalStatus = "pending",
        note: str | None = None,
        id: str | None = None,
        task_id: str | None = None,
    ) -> ApprovalRecord:
        """Create an approval request."""
        apr_id = id if id is not None else new_id("apr")
        created_at = now_iso()
        decided_at = (
            created_at if status in ("approved", "rejected", "cancelled", "expired") else None
        )

        risk_class_val = risk_class.value if isinstance(risk_class, RiskClass) else str(risk_class)
        preview_str = preview.model_dump_json()

        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                insert(approvals).values(
                    id=apr_id,
                    turn_id=turn_id,
                    session_id=session_id,
                    message_id=message_id,
                    agent=agent,
                    tool_call_id=tool_call_id,
                    tool=tool,
                    risk_class=risk_class_val,
                    preview=preview_str,
                    status=status,
                    note=note,
                    created_at=created_at,
                    decided_at=decided_at,
                    task_id=task_id,
                )
            )

        rec = await self.get(project_id, apr_id)
        if rec is None:
            raise RuntimeError(f"Failed to load created approval {apr_id}")
        return rec

    async def get(self, project_id: str, approval_id: str) -> ApprovalRecord | None:
        """Fetch an approval by ID."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (await conn.execute(select(approvals).where(approvals.c.id == approval_id)))
                .mappings()
                .first()
            )
        if row is None:
            return None
        return _row_to_record(row)

    async def list_for_step(
        self,
        project_id: str,
        turn_id: str | Sequence[str] | None = None,
        *,
        approval_ids: Sequence[str] | None = None,
        tool_call_ids: Sequence[str] | None = None,
    ) -> list[ApprovalRecord]:
        """List approvals for a step or turn, optionally filtering by approval_ids or tool_call_ids."""
        if isinstance(turn_id, (list, tuple, set)):
            approval_ids = list(turn_id)
            turn_id = None

        query = select(approvals)
        if turn_id is not None:
            query = query.where(approvals.c.turn_id == turn_id)
        if approval_ids is not None:
            query = query.where(approvals.c.id.in_(approval_ids))
        if tool_call_ids is not None:
            query = query.where(approvals.c.tool_call_id.in_(tool_call_ids))

        query = query.order_by(approvals.c.created_at.asc(), approvals.c.id.asc())

        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [_row_to_record(r) for r in rows]

    async def update_status(
        self,
        project_id: str,
        approval_id: str,
        status: ApprovalStatus,
        *,
        note: str | None = None,
        decided_at: str | None = None,
        task_id: str | object | None = _UNSET,
    ) -> ApprovalRecord:
        """Update an approval's status conditionally WHERE status = 'pending'."""
        engine = await self._databases.project(project_id)
        values: dict[str, Any] = {"status": status}
        if note is not None:
            values["note"] = note
        if status in ("approved", "rejected", "cancelled", "expired"):
            values["decided_at"] = decided_at or now_iso()
        elif status == "pending":
            values["decided_at"] = None

        if task_id is not _UNSET:
            values["task_id"] = task_id

        async with engine.begin() as conn:
            result = await conn.execute(
                update(approvals)
                .where(approvals.c.id == approval_id, approvals.c.status == "pending")
                .values(**values)
            )
            if result.rowcount == 0:
                existing = await self.get(project_id, approval_id)
                if existing is None:
                    raise DomainError(
                        "approval_not_found",
                        f"Approval '{approval_id}' does not exist.",
                        status=404,
                    )
                raise DomainError(
                    "approval_not_pending",
                    f"Approval '{approval_id}' is not pending (status: {existing.status}).",
                    status=409,
                )

        rec = await self.get(project_id, approval_id)
        if rec is None:
            raise DomainError(
                "approval_not_found", f"Approval '{approval_id}' does not exist.", status=404
            )
        return rec

    async def decide(
        self,
        project_id: str,
        approval_id: str,
        *,
        approve: bool,
        note: str | None = None,
        task_id: str | object | None = _UNSET,
    ) -> ApprovalRecord:
        """Approve or reject an approval with an optional note."""
        status: ApprovalStatus = "approved" if approve else "rejected"
        return await self.update_status(project_id, approval_id, status, note=note, task_id=task_id)

    async def pending(self, project_id: str) -> list[ApprovalRecord]:
        """List all pending approvals for a project."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(approvals)
                        .where(approvals.c.status == "pending")
                        .order_by(approvals.c.created_at.asc(), approvals.c.id.asc())
                    )
                )
                .mappings()
                .all()
            )
        return [_row_to_record(r) for r in rows]

    async def cancel_for_turn(self, project_id: str, turn_id: str) -> list[ApprovalRecord]:
        """Cancel all pending approvals for a turn. Returns the cancelled records."""
        now = now_iso()
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            pending_rows = (
                (
                    await conn.execute(
                        select(approvals).where(
                            approvals.c.turn_id == turn_id, approvals.c.status == "pending"
                        )
                    )
                )
                .mappings()
                .all()
            )
            if pending_rows:
                pending_ids = [str(r["id"]) for r in pending_rows]
                await conn.execute(
                    update(approvals)
                    .where(approvals.c.id.in_(pending_ids), approvals.c.status == "pending")
                    .values(status="cancelled", decided_at=now)
                )
        if not pending_rows:
            return []
        return await self.list_for_step(
            project_id, turn_id, approval_ids=[str(r["id"]) for r in pending_rows]
        )

    async def expire_non_waiting(self, project_id: str) -> list[ApprovalRecord]:
        """Expire all pending approvals whose turn is not in 'waiting' status."""
        now = now_iso()
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            query = (
                select(approvals)
                .select_from(approvals.outerjoin(turns, approvals.c.turn_id == turns.c.id))
                .where(
                    approvals.c.status == "pending",
                    or_(turns.c.status.is_(None), turns.c.status != "waiting"),
                )
            )
            rows = (await conn.execute(query)).mappings().all()
            if rows:
                ids = [str(r["id"]) for r in rows]
                await conn.execute(
                    update(approvals)
                    .where(approvals.c.id.in_(ids), approvals.c.status == "pending")
                    .values(status="expired", decided_at=now)
                )
        if not rows:
            return []
        return await self.list_for_step(project_id, approval_ids=[str(r["id"]) for r in rows])
