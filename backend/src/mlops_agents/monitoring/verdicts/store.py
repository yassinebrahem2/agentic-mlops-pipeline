"""SQLite-backed Verdicts (I31): a person's verdicts, miss reports and confirmation by labels."""

from __future__ import annotations

import builtins
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Final, cast, get_args

from sqlalchemy import ColumnElement, RowMapping, func, insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.clock import to_iso
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog
from mlops_agents.monitoring.incidents.contract import Incidents, IncidentStatus, IncidentView
from mlops_agents.monitoring.incidents.service import DONE_STATUSES
from mlops_agents.monitoring.incidents.store import parse_dt
from mlops_agents.monitoring.results.constants import QUERY_LIMIT_MAX
from mlops_agents.monitoring.results.contract import ResultQuery, Results, StoredResult
from mlops_agents.monitoring.verdicts.contract import (
    MissReport,
    VerdictDraft,
    Verdicts,
    VerdictValue,
    VerdictView,
)
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import misses, verdicts
from mlops_agents.tracking.contract import Page

# Confirmation by labels looks at every incident that can still take a verdict from labels.
_LABEL_STATUSES: Final[list[IncidentStatus]] = [
    s for s in get_args(IncidentStatus) if s not in DONE_STATUSES
]


def _row_to_view(row: RowMapping | Mapping[str, Any]) -> VerdictView:
    return VerdictView(
        incident_id=str(row["incident_id"]),
        value=row["value"],
        right_category=row["right_category"],
        right_subtype=row["right_subtype"],
        source=row["source"],
        check_versions=cast(dict[str, str], json.loads(str(row["check_versions"]))),
        note=row["note"],
        created=parse_dt(str(row["created"])),
    )


def _row_values(view: VerdictView) -> dict[str, Any]:
    return {
        "incident_id": view.incident_id,
        "value": view.value,
        "right_category": view.right_category,
        "right_subtype": view.right_subtype,
        "source": view.source,
        "check_versions": json.dumps(view.check_versions, sort_keys=True),
        "note": view.note,
        "created": to_iso(view.created),
    }


def is_label_evidence(result: StoredResult) -> bool:
    """A label-based result that can decide: an agent's model performance check. A
    not-enough-data record is the minimum sample not being met: it never decides."""
    return (
        result.origin == "agent"
        and result.category == "model_performance"
        and not result.not_enough_data
    )


def _latest_per_check(found: Iterable[StoredResult]) -> builtins.list[StoredResult]:
    latest: dict[str, StoredResult] = {}
    for result in found:
        held = latest.get(result.check_id)
        if held is None or (result.ran_at, result.id) > (held.ran_at, held.id):
            latest[result.check_id] = result
    return builtins.list(latest.values())


def _versions(found: Iterable[StoredResult]) -> dict[str, str]:
    return {r.check_id: r.version for r in _latest_per_check(found) if r.version is not None}


class SqliteVerdicts(Verdicts):
    def __init__(
        self,
        databases: Databases,
        incidents: Incidents,
        results: Results,
        events: EventLog,
        clock: Clock,
    ) -> None:
        self._databases = databases
        self._incidents = incidents
        self._results = results
        self._events = events
        self._clock = clock

    async def record(
        self, project_id: str, incident_id: str, draft: VerdictDraft, by: str
    ) -> VerdictView:
        incident = await self._incidents.get(project_id, incident_id)
        if incident is None:
            raise DomainError(
                "incident_not_found", f"Incident '{incident_id}' not found.", status=404
            )
        view = VerdictView(
            incident_id=incident_id,
            value=draft.value,
            right_category=draft.right_category,
            right_subtype=draft.right_subtype,
            source="user",
            check_versions=await self._signal_versions(project_id, incident_id),
            note=draft.note,
            created=self._now(),
        )
        values = _row_values(view)
        stmt = sqlite_insert(verdicts).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[verdicts.c.incident_id],
            set_={k: v for k, v in values.items() if k != "incident_id"},
        )
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(stmt)
        await self._recorded(project_id, view, source=by)
        # S4 §5: a resolved incident is closed after the verdict, and the verdict is the
        # person's act; any other status stays as it is.
        if incident.status == "resolved":
            await self._incidents.close(project_id, incident_id, by)
        return view

    async def get(self, project_id: str, incident_id: str) -> VerdictView | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (await conn.execute(select(verdicts).where(verdicts.c.incident_id == incident_id)))
                .mappings()
                .first()
            )
        return _row_to_view(row) if row is not None else None

    async def confirm_by_labels(self, project_id: str) -> builtins.list[VerdictView]:
        recorded: builtins.list[VerdictView] = []
        for incident in await self._label_candidates(project_id):
            stored = await self.get(project_id, incident.id)
            if stored is not None and stored.source == "user":
                continue
            view = await self._labels_verdict(project_id, incident)
            if view is not None and await self._write_labels(project_id, view):
                await self._recorded(project_id, view, source="labels")
                recorded.append(view)
        return recorded

    async def report_miss(self, project_id: str, miss: MissReport) -> None:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                insert(misses).values(
                    description=miss.description,
                    category=miss.category,
                    occurred_at=to_iso(miss.occurred_at),
                    reported_at=to_iso(miss.reported_at),
                )
            )
        # The description is the team's free text: it stays in the table, out of the event.
        await self._events.record(
            project_id,
            source="user",
            type="miss.reported",
            payload={"category": miss.category, "occurred_at": to_iso(miss.occurred_at)},
        )

    async def list_misses(
        self, project_id: str, since: datetime | None = None
    ) -> builtins.list[MissReport]:
        """Miss reports, newest `reported_at` first; `since` is inclusive."""
        stmt = select(misses).order_by(misses.c.reported_at.desc(), misses.c.id.desc())
        if since is not None:
            stmt = stmt.where(misses.c.reported_at >= to_iso(since))
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [
            MissReport(
                description=str(r["description"]),
                category=r["category"],
                occurred_at=parse_dt(str(r["occurred_at"])),
                reported_at=parse_dt(str(r["reported_at"])),
            )
            for r in rows
        ]

    async def page(
        self,
        project_id: str,
        *,
        incident_id: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> Page[VerdictView]:
        """Verdicts, newest `created` first; `since` is inclusive; no `limit` reads them all."""
        filters: builtins.list[ColumnElement[bool]] = []
        if incident_id is not None:
            filters.append(verdicts.c.incident_id == incident_id)
        if since is not None:
            filters.append(verdicts.c.created >= to_iso(since))
        stmt = (
            select(verdicts)
            .where(*filters)
            .order_by(verdicts.c.created.desc(), verdicts.c.incident_id.desc())
            .limit(limit)
            .offset(offset)
        )
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            total = int(
                (await conn.scalar(select(func.count()).select_from(verdicts).where(*filters))) or 0
            )
            rows = (await conn.execute(stmt)).mappings().all()
        views = [_row_to_view(r) for r in rows]
        return Page(rows=views, total=total, truncated=(offset + len(views)) < total)

    async def list(
        self, project_id: str, since: datetime | None = None
    ) -> builtins.list[VerdictView]:
        return (await self.page(project_id, since=since)).rows

    def _now(self) -> datetime:
        # At the precision the table stores, so a returned verdict equals the one read back.
        return parse_dt(to_iso(self._clock.now()))

    async def _recorded(self, project_id: str, view: VerdictView, *, source: str) -> None:
        await self._events.record(
            project_id,
            source=source,
            type="verdict.recorded",
            payload={"value": view.value, "source": view.source},
            subject_id=view.incident_id,
        )

    async def _all_results(self, project_id: str, q: ResultQuery) -> builtins.list[StoredResult]:
        found: builtins.list[StoredResult] = []
        offset = 0
        while True:
            page = await self._results.query(
                project_id, q.model_copy(update={"limit": QUERY_LIMIT_MAX, "offset": offset})
            )
            found.extend(page.rows)
            if not page.truncated or not page.rows:
                return found
            offset += len(page.rows)

    async def _signal_versions(self, project_id: str, incident_id: str) -> dict[str, str]:
        """`check_id -> version` of the stored results behind the incident's signals."""
        signals = await self._incidents.signals(project_id, incident_id)
        ids = [s.result_id for s in signals if s.result_id is not None]
        found: builtins.list[StoredResult] = []
        for start in range(0, len(ids), QUERY_LIMIT_MAX):
            chunk = ids[start : start + QUERY_LIMIT_MAX]
            found.extend(await self._all_results(project_id, ResultQuery(ids=chunk)))
        return _versions(found)

    async def _label_candidates(self, project_id: str) -> builtins.list[IncidentView]:
        candidates: builtins.list[IncidentView] = []
        offset = 0
        while True:
            page = await self._incidents.list(
                project_id, status=_LABEL_STATUSES, limit=QUERY_LIMIT_MAX, offset=offset
            )
            candidates.extend(
                i for i in page.rows if i.category == "model_performance" and i.model is not None
            )
            if not page.truncated or not page.rows:
                return candidates
            offset += len(page.rows)

    async def _labels_verdict(self, project_id: str, incident: IncidentView) -> VerdictView | None:
        """The verdict the label-based checks give, or None when no result decides."""
        found = await self._all_results(
            project_id,
            ResultQuery(model=incident.model, origin="agent", since=incident.started_at),
        )
        evidence = [r for r in found if is_label_evidence(r) and r.ran_at > incident.started_at]
        if not evidence:
            return None
        latest = _latest_per_check(evidence)
        value: VerdictValue = "real_issue" if any(r.fired for r in latest) else "not_an_issue"
        return VerdictView(
            incident_id=incident.id,
            value=value,
            source="labels",
            check_versions=_versions(latest),
            created=self._now(),
        )

    async def _write_labels(self, project_id: str, view: VerdictView) -> bool:
        """Writes a labels verdict unless a person's verdict or the same outcome is stored.

        One conditional upsert, so a person's verdict recorded meanwhile is never overwritten.
        """
        stmt = sqlite_insert(verdicts).values(**_row_values(view))
        stmt = stmt.on_conflict_do_update(
            index_elements=[verdicts.c.incident_id],
            set_={
                "value": stmt.excluded.value,
                "right_category": None,
                "right_subtype": None,
                "check_versions": stmt.excluded.check_versions,
                "note": None,
                "created": stmt.excluded.created,
            },
            where=(verdicts.c.source == "labels") & (verdicts.c.value != stmt.excluded.value),
        ).returning(verdicts.c.incident_id)
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            written = (await conn.execute(stmt)).first()
        return written is not None
