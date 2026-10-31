"""Monitoring query tool and query providers for signals and incidents (I33)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.sql.elements import ColumnElement

from mlops_agents.agents.tools.base import (
    AgentTool,
    RiskClass,
    ToolContext,
    ToolOutput,
)
from mlops_agents.agents.tools.names import MONITORING_QUERY
from mlops_agents.monitoring.incidents.contract import IncidentView, SignalView
from mlops_agents.monitoring.incidents.store import row_to_incident_view, row_to_signal_view
from mlops_agents.monitoring.query import MonitoringQuery, QueryProviders, QueryWhat
from mlops_agents.monitoring.untrusted import untrusted
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import incidents, signals
from mlops_agents.tracking.contract import Page


class MonitoringQueryTool(AgentTool[MonitoringQuery]):
    name: ClassVar[str] = MONITORING_QUERY
    description: ClassVar[str] = "Query monitoring data (signals, incidents, etc.)."
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = MonitoringQuery

    def __init__(self, providers: QueryProviders) -> None:
        self._providers = providers

    def parse(self, raw_args: Mapping[str, Any]) -> MonitoringQuery | ToolOutput:
        raw_dict = dict(raw_args)
        limit = raw_dict.get("limit")
        if isinstance(limit, (int, float)) and limit > 200:
            return ToolOutput(content="limit above 200 is refused.", is_error=True)
        return super().parse(raw_args)

    async def run(self, ctx: ToolContext, args: MonitoringQuery) -> ToolOutput:
        if args.limit > 200:
            return ToolOutput(content="limit above 200 is refused.", is_error=True)

        provider = self._providers.get(args.what)
        if provider is None:
            return ToolOutput(content=f"nothing is recorded for {args.what} yet")

        page = await provider.query(ctx.project_id, args)

        rows_data = [
            row.model_dump(mode="json") if hasattr(row, "model_dump") else row for row in page.rows
        ]
        rows_json = json.dumps(rows_data, indent=2, default=str)
        content = (
            f"total: {page.total}\n"
            f"truncated: {page.truncated}\n"
            f"{untrusted(rows_json, source='monitoring_query')}"
        )
        return ToolOutput(content=content)


class SignalsQueryProvider:
    what: QueryWhat = "signals"

    def __init__(self, databases: Databases) -> None:
        self._databases = databases

    async def query(self, project_id: str, q: MonitoringQuery) -> Page[SignalView]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = select(signals)
            count_stmt = select(func.count(signals.c.id))
            conditions: list[ColumnElement[bool]] = []
            if q.id is not None:
                conditions.append(signals.c.id == q.id)
            if q.incident_id is not None:
                conditions.append(signals.c.incident_id == q.incident_id)
            if q.model is not None:
                conditions.append(signals.c.model == q.model)
            if q.subject is not None:
                conditions.append(signals.c.subject == q.subject)
            if q.since is not None:
                conditions.append(signals.c.ran_at >= q.since.isoformat())

            if conditions:
                stmt = stmt.where(*conditions)
                count_stmt = count_stmt.where(*conditions)

            total_res = await conn.execute(count_stmt)
            total = total_res.scalar() or 0

            stmt = (
                stmt.order_by(signals.c.ran_at.desc(), signals.c.id.desc())
                .limit(q.limit)
                .offset(q.offset)
            )
            res = await conn.execute(stmt)
            rows = [row_to_signal_view(r) for r in res.mappings().all()]
            truncated = (q.offset + len(rows)) < total
            return Page(rows=rows, total=total, truncated=truncated)


class IncidentsQueryProvider:
    what: QueryWhat = "incidents"

    def __init__(self, databases: Databases) -> None:
        self._databases = databases

    async def query(self, project_id: str, q: MonitoringQuery) -> Page[IncidentView]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = select(incidents)
            count_stmt = select(func.count(incidents.c.id))
            conditions: list[ColumnElement[bool]] = []
            if q.id is not None:
                conditions.append(incidents.c.id == q.id)
            if q.status is not None:
                conditions.append(incidents.c.status == q.status)
            if q.model is not None:
                conditions.append(incidents.c.model == q.model)
            if q.since is not None:
                conditions.append(incidents.c.detected_at >= q.since.isoformat())

            if conditions:
                stmt = stmt.where(*conditions)
                count_stmt = count_stmt.where(*conditions)

            total_res = await conn.execute(count_stmt)
            total = total_res.scalar() or 0

            severity_rank = case(
                (incidents.c.severity == "critical", 4),
                (incidents.c.severity == "high", 3),
                (incidents.c.severity == "medium", 2),
                (incidents.c.severity == "low", 1),
                else_=0,
            )
            stmt = (
                stmt.order_by(severity_rank.desc(), incidents.c.detected_at.desc())
                .limit(q.limit)
                .offset(q.offset)
            )
            res = await conn.execute(stmt)
            rows = res.mappings().all()

            inc_ids = [str(r["id"]) for r in rows]
            sig_map: dict[str, list[str]] = {i: [] for i in inc_ids}
            if inc_ids:
                sig_res = await conn.execute(
                    select(signals.c.id, signals.c.incident_id)
                    .where(signals.c.incident_id.in_(inc_ids))
                    .order_by(signals.c.ran_at.asc(), signals.c.id.asc())
                )
                for srow in sig_res.mappings():
                    sig_map[str(srow["incident_id"])].append(str(srow["id"]))

            views: list[IncidentView] = [
                row_to_incident_view(r, sig_map.get(str(r["id"]), [])) for r in rows
            ]
            truncated = (q.offset + len(views)) < total
            return Page(rows=views, total=total, truncated=truncated)
