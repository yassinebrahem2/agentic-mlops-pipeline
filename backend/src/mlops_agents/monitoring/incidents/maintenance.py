"""Maintenance windows, proposal kind, and windows query provider (I25, I32, I33)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy import RowMapping, delete, insert, select

from mlops_agents.agents.tools.base import Preview
from mlops_agents.clock import as_utc, to_iso
from mlops_agents.errors import DomainError
from mlops_agents.ids import new_id
from mlops_agents.monitoring.expectations.contract import ExpectationKey, Maintenance
from mlops_agents.monitoring.incidents.contract import MaintenancePayload, WindowSpec
from mlops_agents.monitoring.incidents.store import parse_dt
from mlops_agents.monitoring.query import MonitoringQuery, QueryWhat
from mlops_agents.proposals.contract import (
    MAINTENANCE_KIND,
    ApplyResult,
    CredentialSlot,
    maintenance_key,
)
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import maintenance_windows
from mlops_agents.tracking.contract import Page


class MaintenanceWindowView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    start: datetime
    end: datetime
    scope: list[str]
    reason: str
    proposal_id: str | None = None
    created_by: str
    created: datetime


def _row_to_view(row: RowMapping | Mapping[str, Any]) -> MaintenanceWindowView:
    return MaintenanceWindowView(
        id=str(row["id"]),
        start=parse_dt(str(row["start"])),
        end=parse_dt(str(row["end"])),
        scope=cast_scope(row["scope"]),
        reason=str(row["reason"]),
        proposal_id=str(row["proposal_id"]) if row["proposal_id"] is not None else None,
        created_by=str(row["created_by"]),
        created=parse_dt(str(row["created"])),
    )


def cast_scope(raw: object) -> list[str]:
    if isinstance(raw, list):
        items = cast(list[object], raw)
        return [str(x) for x in items]
    if isinstance(raw, str):
        try:
            parsed: object = json.loads(raw)
            if isinstance(parsed, list):
                items = cast(list[object], parsed)
                return [str(x) for x in items]
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
    return []


class MaintenanceWindows(Maintenance):
    """Stores and evaluates maintenance windows."""

    def __init__(self, databases: Databases, clock: Clock) -> None:
        self._databases = databases
        self._clock = clock

    async def suspended(self, project_id: str, scope: ExpectationKey | str, at: datetime) -> bool:
        at_iso = to_iso(at)
        target = scope.ref if isinstance(scope, ExpectationKey) else scope

        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = select(maintenance_windows).where(
                maintenance_windows.c.start <= at_iso,
                maintenance_windows.c.end > at_iso,
            )
            res = await conn.execute(stmt)
            rows = res.mappings().all()

        for row in rows:
            window_scope = cast_scope(row["scope"])
            if target in window_scope:
                return True
        return False

    async def create(
        self,
        project_id: str,
        spec: WindowSpec,
        created_by: str,
        proposal_id: str | None = None,
    ) -> MaintenanceWindowView:
        # as_utc: comparing a naive time with an aware one raises.
        start, end = as_utc(spec.start), as_utc(spec.end)
        if start >= end:
            raise DomainError(
                "window_inert", "A maintenance window must start before it ends.", status=422
            )
        if not spec.scope:
            raise DomainError(
                "window_inert", "A maintenance window needs at least one scope.", status=422
            )
        win_id = new_id("win")
        created_dt = self._clock.now()
        created_iso = to_iso(created_dt)
        start_iso = to_iso(spec.start)
        end_iso = to_iso(spec.end)
        scope_json = json.dumps(spec.scope)

        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            stmt = insert(maintenance_windows).values(
                id=win_id,
                start=start_iso,
                end=end_iso,
                scope=scope_json,
                reason=spec.reason,
                proposal_id=proposal_id,
                created_by=created_by,
                created=created_iso,
            )
            await conn.execute(stmt)

        return MaintenanceWindowView(
            id=win_id,
            start=parse_dt(start_iso),
            end=parse_dt(end_iso),
            scope=list(spec.scope),
            reason=spec.reason,
            proposal_id=proposal_id,
            created_by=created_by,
            created=parse_dt(created_iso),
        )

    async def get(self, project_id: str, window_id: str) -> MaintenanceWindowView | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = select(maintenance_windows).where(maintenance_windows.c.id == window_id)
            res = await conn.execute(stmt)
            row = res.mappings().first()
            if row is None:
                return None
            return _row_to_view(row)

    async def get_by_proposal_id(
        self, project_id: str, proposal_id: str
    ) -> MaintenanceWindowView | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = select(maintenance_windows).where(
                maintenance_windows.c.proposal_id == proposal_id
            )
            res = await conn.execute(stmt)
            row = res.mappings().first()
            if row is None:
                return None
            return _row_to_view(row)

    async def list(self, project_id: str) -> list[MaintenanceWindowView]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = select(maintenance_windows).order_by(
                maintenance_windows.c.created.desc(),
                maintenance_windows.c.id.desc(),
            )
            res = await conn.execute(stmt)
            return [_row_to_view(r) for r in res.mappings().all()]

    async def delete(self, project_id: str, window_id: str) -> None:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            stmt = delete(maintenance_windows).where(maintenance_windows.c.id == window_id)
            res = await conn.execute(stmt)
            if res.rowcount == 0:
                raise DomainError(
                    "window_not_found",
                    f"Maintenance window '{window_id}' not found.",
                    status=404,
                )


def _payload(payload: BaseModel) -> MaintenancePayload:
    if isinstance(payload, MaintenancePayload):
        return payload
    return MaintenancePayload.model_validate(payload)


class MaintenanceKind:
    """ProposalKind implementation for maintenance windows."""

    name: str = MAINTENANCE_KIND
    payload_model: type[BaseModel] = MaintenancePayload

    def __init__(self, windows: MaintenanceWindows) -> None:
        self._windows = windows

    def key(self, payload: BaseModel) -> str:
        return maintenance_key(_payload(payload))

    def preview(self, payload: BaseModel) -> Preview:
        p = _payload(payload)
        lines = [
            f"Window: {to_iso(p.window.start)} to {to_iso(p.window.end)}",
            f"Scope: {', '.join(p.window.scope)}",
            f"Reason: {p.window.reason}",
        ]
        return Preview(kind="text", title="Maintenance Window", body="\n".join(lines))

    def warnings(self, payload: BaseModel) -> list[str]:
        return []

    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]:
        return []

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        existing = await self._windows.get_by_proposal_id(project_id, proposal_id)
        if existing is not None:
            return ApplyResult(applied=[f"window:{existing.id}"], failed={})

        p = _payload(payload)
        try:
            win = await self._windows.create(
                project_id,
                p.window,
                created_by="proposal",
                proposal_id=proposal_id,
            )
            return ApplyResult(applied=[f"window:{win.id}"], failed={})
        except Exception:
            existing = await self._windows.get_by_proposal_id(project_id, proposal_id)
            if existing is not None:
                return ApplyResult(applied=[f"window:{existing.id}"], failed={})
            raise


class WindowsQueryProvider:
    """QueryProvider for maintenance windows."""

    what: QueryWhat = "windows"

    def __init__(self, windows: MaintenanceWindows) -> None:
        self._windows = windows

    async def query(self, project_id: str, q: MonitoringQuery) -> Page[MaintenanceWindowView]:
        all_windows = await self._windows.list(project_id)
        if q.id is not None:
            all_windows = [w for w in all_windows if w.id == q.id]
        if q.since is not None:
            all_windows = [w for w in all_windows if w.created >= q.since]

        total = len(all_windows)
        rows = all_windows[q.offset : q.offset + q.limit]
        truncated = (q.offset + len(rows)) < total
        return Page(rows=rows, total=total, truncated=truncated)
