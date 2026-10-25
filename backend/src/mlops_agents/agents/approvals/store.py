"""Approval storage backed by project.db (I12)."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from mlops_agents.agents.approvals.contract import ApprovalView
from mlops_agents.agents.runtime.contract import ApprovalStatus
from mlops_agents.agents.tools.base import Preview, RiskClass
from mlops_agents.clock import now_iso
from mlops_agents.ids import new_id
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import approvals


class ApprovalRecord(ApprovalView):
    """Database representation of an approval request, including its message_id."""

    message_id: str
    task_id: str | None = None


def _row_to_record(row: Any) -> ApprovalRecord:
    preview_raw = row["preview"]
    task_id: str | None = None
    if isinstance(preview_raw, str):
        with contextlib.suppress(Exception):
            raw_parsed: object = json.loads(preview_raw)
            if isinstance(raw_parsed, dict):
                raw_dict: dict[str, object] = cast(dict[str, object], raw_parsed)
                raw_val = raw_dict.get("task_id")
                if raw_val is not None:
                    task_id = str(raw_val)
        preview_obj = Preview.model_validate_json(preview_raw)
    elif isinstance(preview_raw, dict):
        raw_dict: dict[str, object] = cast(dict[str, object], preview_raw)
        raw_val = raw_dict.get("task_id")
        if raw_val is not None:
            task_id = str(raw_val)
        preview_obj = Preview.model_validate(preview_raw)
    else:
        preview_obj = cast(Preview, preview_raw)

    risk_class_raw = row["risk_class"]
    risk_class_obj = (
        RiskClass(risk_class_raw) if not isinstance(risk_class_raw, RiskClass) else risk_class_raw
    )

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
        preview: Preview | dict[str, Any] | str,
        status: ApprovalStatus = "pending",
        note: str | None = None,
        id: str | None = None,
        task_id: str | None = None,
    ) -> ApprovalRecord:
        """Create an approval request. Idempotent by tool_call_id."""
        existing = await self.find_by_tool_call_id(project_id, tool_call_id)
        if existing is not None:
            return existing

        apr_id = id if id is not None else new_id("apr")
        created_at = now_iso()
        decided_at = (
            created_at if status in ("approved", "rejected", "cancelled", "expired") else None
        )

        risk_class_val = risk_class.value if isinstance(risk_class, RiskClass) else str(risk_class)
        if isinstance(preview, Preview):
            preview_dict = preview.model_dump()
        elif isinstance(preview, dict):
            preview_dict = dict(preview)
        else:
            try:
                preview_dict = json.loads(str(preview))
            except Exception:
                preview_dict = {"kind": "text", "title": tool, "body": str(preview)}
        if task_id is not None:
            preview_dict["task_id"] = task_id
        preview_str = json.dumps(preview_dict)

        engine = await self._databases.project(project_id)
        try:
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
                    )
                )
        except IntegrityError:
            existing_after_race = await self.find_by_tool_call_id(project_id, tool_call_id)
            if existing_after_race is not None:
                return existing_after_race
            raise

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

    async def find_by_tool_call_id(
        self, project_id: str, tool_call_id: str
    ) -> ApprovalRecord | None:
        """Find an approval by tool_call_id."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(approvals).where(approvals.c.tool_call_id == tool_call_id)
                    )
                )
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
        task_id: str | None = None,
    ) -> ApprovalRecord | None:
        """Update an approval's status and optional note."""
        engine = await self._databases.project(project_id)
        values: dict[str, Any] = {"status": status}
        if note is not None:
            values["note"] = note
        if status in ("approved", "rejected", "cancelled", "expired"):
            values["decided_at"] = decided_at or now_iso()
        elif status == "pending":
            values["decided_at"] = None

        if task_id is not None:
            rec = await self.get(project_id, approval_id)
            if rec is not None:
                p_dict = rec.preview.model_dump()
                p_dict["task_id"] = task_id
                values["preview"] = json.dumps(p_dict)

        async with engine.begin() as conn:
            await conn.execute(
                update(approvals).where(approvals.c.id == approval_id).values(**values)
            )
        return await self.get(project_id, approval_id)

    async def decide(
        self,
        project_id: str,
        approval_id: str,
        *,
        approve: bool,
        note: str | None = None,
        task_id: str | None = None,
    ) -> ApprovalRecord:
        """Approve or reject an approval with an optional note."""
        status: ApprovalStatus = "approved" if approve else "rejected"
        updated = await self.update_status(
            project_id, approval_id, status, note=note, task_id=task_id
        )
        if updated is None:
            raise KeyError(f"Approval not found: {approval_id}")
        return updated

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
