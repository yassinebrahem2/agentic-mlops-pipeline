"""Database operations and row <-> view mappings for signals and incidents."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import RowMapping, and_, case, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.monitoring.expectations.contract import ExpectationKey
from mlops_agents.monitoring.incidents.contract import (
    ACTIVE_STATUSES,
    IncidentStatus,
    IncidentView,
    SignalDraft,
    SignalView,
)
from mlops_agents.monitoring.verdicts.contract import VerdictView
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import events, incidents, recent_changes, signals, verdicts


def parse_dt(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def row_to_signal_view(row: RowMapping | Mapping[str, Any]) -> SignalView:
    expectation_data = row["expectation"]
    expectation: ExpectationKey | None = None
    if expectation_data:
        expectation = ExpectationKey.model_validate_json(expectation_data)

    return SignalView(
        id=str(row["id"]),
        incident_id=str(row["incident_id"]),
        kind=row["kind"],
        category=row["category"],
        subject=str(row["subject"]),
        model=row["model"],
        ran_at=parse_dt(str(row["ran_at"])),
        seen_at=parse_dt(str(row["seen_at"])),
        cause_key=str(row["cause_key"]),
        detail=json.loads(str(row["detail"])),
        result_id=row["result_id"],
        source_ref=row["source_ref"],
        expectation=expectation,
        cleared=bool(row["cleared"]),
    )


def row_to_incident_view(
    row: RowMapping | Mapping[str, Any],
    signal_ids: list[str],
    verdict: VerdictView | None = None,
) -> IncidentView:
    prs_raw = row["prs"]
    issues_raw = row["issues"]
    prs: list[int] = cast(list[int], json.loads(str(prs_raw))) if prs_raw else []
    issues: list[int] = cast(list[int], json.loads(str(issues_raw))) if issues_raw else []

    ack_raw = row["acknowledged_at"]
    mit_raw = row["mitigated_at"]
    res_raw = row["resolved_at"]
    cls_raw = row["closed_at"]
    snz_raw = row["snoozed_until"]

    return IncidentView(
        id=str(row["id"]),
        category=row["category"],
        subtype=row["subtype"],
        severity=row["severity"],
        status=row["status"],
        cause=row["cause"],
        cause_key=str(row["cause_key"]),
        model=row["model"],
        signal_ids=signal_ids,
        prs=prs,
        issues=issues,
        held=bool(row["held"]),
        merged_into=row["merged_into"],
        started_at=parse_dt(str(row["started_at"])),
        detected_at=parse_dt(str(row["detected_at"])),
        acknowledged_at=parse_dt(str(ack_raw)) if ack_raw else None,
        mitigated_at=parse_dt(str(mit_raw)) if mit_raw else None,
        resolved_at=parse_dt(str(res_raw)) if res_raw else None,
        closed_at=parse_dt(str(cls_raw)) if cls_raw else None,
        snoozed_until=parse_dt(str(snz_raw)) if snz_raw else None,
        resolved_offline=bool(row["resolved_offline"]),
        reopened_count=int(row["reopened_count"]),
        verdict=verdict,
    )


class IncidentStore:
    def __init__(self, databases: Databases) -> None:
        self._databases = databases

    async def insert_signal(
        self,
        project_id: str,
        signal_id: str,
        incident_id: str,
        draft: SignalDraft,
        seen_at: datetime,
        cause_key: str,
    ) -> SignalView:
        exp_json = draft.expectation.model_dump_json() if draft.expectation else None
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                signals.insert().values(
                    id=signal_id,
                    incident_id=incident_id,
                    kind=draft.kind,
                    category=draft.category,
                    subject=draft.subject,
                    model=draft.model,
                    ran_at=draft.ran_at.isoformat(),
                    seen_at=seen_at.isoformat(),
                    cause_key=cause_key,
                    detail=json.dumps(draft.detail),
                    result_id=draft.result_id,
                    source_ref=draft.source_ref,
                    expectation=exp_json,
                    cleared=1 if draft.cleared else 0,
                )
            )
        return SignalView(
            id=signal_id,
            incident_id=incident_id,
            kind=draft.kind,
            category=draft.category,
            subject=draft.subject,
            model=draft.model,
            ran_at=draft.ran_at,
            seen_at=seen_at,
            cause_key=cause_key,
            detail=draft.detail,
            result_id=draft.result_id,
            source_ref=draft.source_ref,
            expectation=draft.expectation,
            cleared=draft.cleared,
        )

    async def get_signal_by_result_id(self, project_id: str, result_id: str) -> SignalView | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            res = await conn.execute(select(signals).where(signals.c.result_id == result_id))
            row = res.mappings().first()
            if row is None:
                return None
            return row_to_signal_view(row)

    async def list_signals_for_incident(
        self, project_id: str, incident_id: str
    ) -> list[SignalView]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            res = await conn.execute(
                select(signals)
                .where(signals.c.incident_id == incident_id)
                .order_by(signals.c.ran_at.asc(), signals.c.id.asc())
            )
            rows = res.mappings().all()
            return [row_to_signal_view(r) for r in rows]

    async def get_signal_ids_for_incident(self, project_id: str, incident_id: str) -> list[str]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            res = await conn.execute(
                select(signals.c.id)
                .where(signals.c.incident_id == incident_id)
                .order_by(signals.c.ran_at.asc(), signals.c.id.asc())
            )
            return [str(r[0]) for r in res.fetchall()]

    async def get_signal_ids_for_incidents(
        self, project_id: str, incident_ids: list[str]
    ) -> dict[str, list[str]]:
        if not incident_ids:
            return {}
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            res = await conn.execute(
                select(signals.c.id, signals.c.incident_id)
                .where(signals.c.incident_id.in_(incident_ids))
                .order_by(signals.c.ran_at.asc(), signals.c.id.asc())
            )
            mapping: dict[str, list[str]] = {inc_id: [] for inc_id in incident_ids}
            for row in res.mappings():
                inc_id = str(row["incident_id"])
                sig_id = str(row["id"])
                if inc_id in mapping:
                    mapping[inc_id].append(sig_id)
            return mapping

    async def move_signals(
        self, project_id: str, signal_ids: list[str], target_incident_id: str
    ) -> None:
        if not signal_ids:
            return
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                update(signals)
                .where(signals.c.id.in_(signal_ids))
                .values(incident_id=target_incident_id)
            )

    async def move_all_signals(
        self, project_id: str, from_incident_ids: list[str], target_incident_id: str
    ) -> None:
        if not from_incident_ids:
            return
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                update(signals)
                .where(signals.c.incident_id.in_(from_incident_ids))
                .values(incident_id=target_incident_id)
            )

    async def insert_incident(self, project_id: str, values: dict[str, Any]) -> None:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(incidents.insert().values(**values))

    async def get_incident_row(self, project_id: str, incident_id: str) -> RowMapping | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            res = await conn.execute(select(incidents).where(incidents.c.id == incident_id))
            return res.mappings().first()

    async def get_active_incident_by_cause_key(
        self, project_id: str, cause_key: str
    ) -> RowMapping | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            res = await conn.execute(
                select(incidents)
                .where(
                    incidents.c.cause_key == cause_key,
                    incidents.c.status.in_(ACTIVE_STATUSES),
                )
                .order_by(incidents.c.detected_at.asc(), incidents.c.started_at.asc())
            )
            return res.mappings().first()

    async def get_active_incidents(self, project_id: str) -> Sequence[RowMapping]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            res = await conn.execute(
                select(incidents)
                .where(incidents.c.status.in_(ACTIVE_STATUSES))
                .order_by(incidents.c.started_at.asc(), incidents.c.id.asc())
            )
            return res.mappings().all()

    async def get_latest_signal_ran_at(self, project_id: str, incident_id: str) -> datetime | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            res = await conn.execute(
                select(signals.c.ran_at)
                .where(signals.c.incident_id == incident_id)
                .order_by(signals.c.ran_at.desc(), signals.c.id.desc())
                .limit(1)
            )
            val = res.scalar()
            return parse_dt(str(val)) if val is not None else None

    async def get_latest_incident_by_cause_key(
        self, project_id: str, cause_key: str
    ) -> RowMapping | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            res = await conn.execute(
                select(incidents)
                .where(incidents.c.cause_key == cause_key)
                .order_by(incidents.c.detected_at.desc(), incidents.c.started_at.desc())
                .limit(1)
            )
            return res.mappings().first()

    async def insert_recent_change(
        self,
        project_id: str,
        kind: str,
        ref: str,
        at: datetime,
        detail: dict[str, Any],
    ) -> None:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            stmt = (
                sqlite_insert(recent_changes)
                .values(
                    kind=kind,
                    ref=ref,
                    at=at.isoformat(),
                    detail=json.dumps(detail),
                )
                .on_conflict_do_nothing(index_elements=["kind", "ref"])
            )
            await conn.execute(stmt)

    async def list_recent_changes(self, project_id: str) -> Sequence[RowMapping]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            res = await conn.execute(select(recent_changes).order_by(recent_changes.c.at.asc()))
            return res.mappings().all()

    async def get_linked_suspects(self, project_id: str, incident_id: str) -> set[tuple[str, str]]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            res = await conn.execute(
                select(events.c.payload).where(
                    events.c.subject_id == incident_id,
                    events.c.type == "incident.suspect_linked",
                )
            )
            linked: set[tuple[str, str]] = set()
            for raw in res.scalars():
                data = json.loads(str(raw))
                kind = data.get("kind")
                ref = data.get("ref")
                if kind is not None and ref is not None:
                    linked.add((str(kind), str(ref)))
            return linked

    async def update_incident_prs(self, project_id: str, incident_id: str, prs: list[int]) -> None:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                update(incidents).where(incidents.c.id == incident_id).values(prs=json.dumps(prs))
            )

    async def update_incident(
        self, project_id: str, incident_id: str, values: dict[str, Any]
    ) -> None:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                update(incidents).where(incidents.c.id == incident_id).values(**values)
            )

    async def list_incidents(
        self,
        project_id: str,
        status: list[IncidentStatus] | None = None,
        limit: int = 50,
        offset: int = 0,
        inbox: bool = False,
    ) -> tuple[Sequence[RowMapping], int]:
        engine = await self._databases.project(project_id)
        base_query = select(incidents)
        count_query = select(func.count(incidents.c.id))

        if inbox:
            has_verdict = (
                select(verdicts.c.incident_id)
                .where(verdicts.c.incident_id == incidents.c.id)
                .exists()
            )
            inbox_filter = or_(
                incidents.c.status.in_(ACTIVE_STATUSES),
                and_(incidents.c.status == "resolved", ~has_verdict),
            )
            base_query = base_query.where(inbox_filter)
            count_query = count_query.where(inbox_filter)
        elif status:
            base_query = base_query.where(incidents.c.status.in_(status))
            count_query = count_query.where(incidents.c.status.in_(status))

        # Severity ranking: critical first (4), then high (3), medium (2), low (1), else 0;
        # then newest detected_at (descending)
        severity_rank = case(
            (incidents.c.severity == "critical", 4),
            (incidents.c.severity == "high", 3),
            (incidents.c.severity == "medium", 2),
            (incidents.c.severity == "low", 1),
            else_=0,
        )
        ranked_query = (
            base_query.order_by(severity_rank.desc(), incidents.c.detected_at.desc())
            .limit(limit)
            .offset(offset)
        )

        async with engine.connect() as conn:
            count_res = await conn.execute(count_query)
            total = count_res.scalar() or 0

            res = await conn.execute(ranked_query)
            rows = res.mappings().all()

            return rows, total

    async def list_held_incidents(self, project_id: str, model: str | None) -> Sequence[RowMapping]:
        engine = await self._databases.project(project_id)
        conditions = [
            incidents.c.held == 1,
            incidents.c.status.not_in(["resolved", "closed", "merged"]),
            incidents.c.hold_scope.is_not(None),
        ]
        if model is not None:
            conditions.append(or_(incidents.c.model == model, incidents.c.model.is_(None)))
        else:
            conditions.append(incidents.c.model.is_(None))

        async with engine.connect() as conn:
            res = await conn.execute(select(incidents).where(*conditions))
            return res.mappings().all()
