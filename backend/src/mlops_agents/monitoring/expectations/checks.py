"""Checks store and PR observer (I25 / I33)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter
from sqlalchemy import select, update

from mlops_agents.agents.pr_sync import PrChange, PrObserver
from mlops_agents.clock import to_iso
from mlops_agents.events.contract import EventLog
from mlops_agents.ids import new_id
from mlops_agents.monitoring.expectations.contract import (
    CheckView,
    ExpectationKey,
    Expectations,
    ExpectationSpec,
)
from mlops_agents.monitoring.query import MonitoringQuery, QueryProvider, QueryWhat
from mlops_agents.monitoring.results.contract import ResultCategory
from mlops_agents.monitoring.scheduler.contract import SyncObserver
from mlops_agents.proposals.payloads import Cadence, FileResults, MlflowResults
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import checks, results, tasks
from mlops_agents.tracking.contract import Page

if TYPE_CHECKING:
    pass

_LOCATION_ADAPTER = TypeAdapter[FileResults | MlflowResults](FileResults | MlflowResults)


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(UTC)


class ChecksStore(Protocol):
    """Protocol for check registration and lookup."""

    async def register(
        self,
        project_id: str,
        *,
        check_id: str,
        category: ResultCategory,
        cadence: Cadence,
        timezone: str,
        results_location: FileResults | MlflowResults,
        pr_number: int,
        agent: str,
    ) -> CheckView: ...

    async def list(self, project_id: str) -> list[CheckView]: ...

    async def list_active(self, project_id: str) -> list[CheckView]: ...

    async def reconcile(self, project_id: str) -> None: ...


class SqliteChecks(ChecksStore, PrObserver, SyncObserver):
    """SQLite-backed checks store and PR merge/close observer."""

    def __init__(
        self,
        databases: Databases,
        expectations: Expectations,
        events: EventLog,
        clock: Clock,
    ) -> None:
        self._databases = databases
        self._expectations = expectations
        self._events = events
        self._clock = clock

    async def _resolve_version(self, conn: Any, check_id: str) -> str | None:
        row = (
            await conn.execute(
                select(results.c.version)
                .where(
                    results.c.check_id == check_id,
                    results.c.version.is_not(None),
                    results.c.version != "",
                )
                .order_by(results.c.ran_at.desc(), results.c.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return row

    async def register(
        self,
        project_id: str,
        *,
        check_id: str,
        category: ResultCategory,
        cadence: Cadence,
        timezone: str,
        results_location: FileResults | MlflowResults,
        pr_number: int,
        agent: str,
    ) -> CheckView:
        # Validate timezone
        try:
            ZoneInfo(timezone)
        except Exception as exc:
            raise ValueError(f"Invalid timezone {timezone!r}") from exc

        engine = await self._databases.project(project_id)
        cadence_json = cadence.model_dump_json()
        location_json = results_location.model_dump_json()
        now_dt = self._clock.now()
        now_iso = to_iso(now_dt)

        chk_id: str
        async with engine.begin() as conn:
            task_status = (
                await conn.execute(
                    select(tasks.c.status).where(tasks.c.pr_number == pr_number).limit(1)
                )
            ).scalar_one_or_none()
            is_pr_merged = task_status == "merged"

            target_status = "active" if is_pr_merged else "pending_pr"

            existing = (
                (await conn.execute(select(checks).where(checks.c.check_id == check_id)))
                .mappings()
                .one_or_none()
            )

            if existing is not None:
                chk_id = str(existing["id"])
                await conn.execute(
                    update(checks)
                    .where(checks.c.id == chk_id)
                    .values(
                        category=str(category),
                        cadence=cadence_json,
                        timezone=timezone,
                        results_location=location_json,
                        pr_number=pr_number,
                        agent=agent,
                        status=target_status,
                        registered=now_iso,
                    )
                )
            else:
                chk_id = new_id("chk")
                await conn.execute(
                    checks.insert().values(
                        id=chk_id,
                        check_id=check_id,
                        category=str(category),
                        cadence=cadence_json,
                        timezone=timezone,
                        results_location=location_json,
                        pr_number=pr_number,
                        agent=agent,
                        version=None,
                        status=target_status,
                        registered=now_iso,
                    )
                )

            version = await self._resolve_version(conn, check_id)

        # Register expectation
        spec = ExpectationSpec(
            key=ExpectationKey(kind="check", ref=check_id),
            category=category,
            cadence=cadence,
            timezone=timezone,
            grace_minutes=None,
            second_deadline=False,
            activates_on_pr=None if is_pr_merged else pr_number,
            source="check",
        )
        await self._expectations.register(project_id, spec)
        if is_pr_merged:
            await self._expectations.activate_for_pr(project_id, pr_number)

        view = CheckView(
            id=chk_id,
            check_id=check_id,
            category=category,
            cadence=cadence,
            timezone=timezone,
            results_location=results_location,
            pr_number=pr_number,
            agent=agent,
            version=version,
            status=target_status,
            registered=now_dt,
        )

        await self._events.record(
            project_id,
            source="checks",
            type="check.registered",
            payload=view.model_dump(mode="json"),
        )
        return view

    async def activate_check(self, project_id: str, pr_number: int) -> None:
        """Idempotently activates expectation before updating check status to active."""
        await self._expectations.activate_for_pr(project_id, pr_number)
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                update(checks)
                .where(
                    checks.c.pr_number == pr_number,
                    checks.c.status == "pending_pr",
                )
                .values(status="active")
            )

    async def reconcile(self, project_id: str) -> None:
        """Repairs check states against merged PR tasks and ensures expectations are active."""
        engine = await self._databases.project(project_id)
        to_activate_prs: set[int] = set()
        async with engine.connect() as conn:
            pending_checks = (
                (
                    await conn.execute(
                        select(checks.c.id, checks.c.pr_number).where(
                            checks.c.status == "pending_pr"
                        )
                    )
                )
                .mappings()
                .all()
            )
            for chk in pending_checks:
                pr_num = chk["pr_number"]
                if pr_num is not None:
                    t_status = (
                        await conn.execute(
                            select(tasks.c.status).where(tasks.c.pr_number == pr_num).limit(1)
                        )
                    ).scalar_one_or_none()
                    if t_status == "merged":
                        to_activate_prs.add(int(pr_num))

        for pr_num in to_activate_prs:
            await self.activate_check(project_id, pr_num)

    async def synced(self, project_id: str, now: datetime) -> None:
        """SyncObserver callback for background reconciliation."""
        await self.reconcile(project_id)

    async def list(self, project_id: str) -> list[CheckView]:
        await self.reconcile(project_id)
        engine = await self._databases.project(project_id)
        views: list[CheckView] = []
        async with engine.connect() as conn:
            rows = (
                (await conn.execute(select(checks).order_by(checks.c.registered.asc())))
                .mappings()
                .all()
            )

            for row in rows:
                version = await self._resolve_version(conn, str(row["check_id"]))
                view = CheckView(
                    id=str(row["id"]),
                    check_id=str(row["check_id"]),
                    category=row["category"],
                    cadence=Cadence.model_validate_json(row["cadence"]),
                    timezone=str(row["timezone"]),
                    results_location=_LOCATION_ADAPTER.validate_json(row["results_location"]),
                    pr_number=int(row["pr_number"]),
                    agent=str(row["agent"]),
                    version=version,
                    status=row["status"],
                    registered=_parse_iso(str(row["registered"])),
                )
                views.append(view)
        return views

    async def list_active(self, project_id: str) -> list[CheckView]:
        all_views = await self.list(project_id)
        return [v for v in all_views if v.status == "active"]

    async def changed(self, project_id: str, pr: PrChange) -> None:
        """PrObserver callback: activate on merge, retire on close unmerged."""
        if pr.state == "merged":
            await self._on_merged(project_id, pr.number)
        elif pr.state == "closed":
            await self._on_closed(project_id, pr.number)

    async def _on_merged(self, project_id: str, pr_number: int) -> None:
        await self.activate_check(project_id, pr_number)

    async def _on_closed(self, project_id: str, pr_number: int) -> None:
        engine = await self._databases.project(project_id)
        check_ids: list[str] = []
        async with engine.begin() as conn:
            rows = (
                (
                    await conn.execute(
                        select(checks.c.check_id).where(
                            checks.c.pr_number == pr_number,
                            checks.c.status == "pending_pr",
                        )
                    )
                )
                .scalars()
                .all()
            )
            check_ids = [str(c) for c in rows]

            if check_ids:
                await conn.execute(
                    update(checks)
                    .where(
                        checks.c.pr_number == pr_number,
                        checks.c.status == "pending_pr",
                    )
                    .values(status="retired")
                )

        for check_id in check_ids:
            await self._expectations.retire(project_id, ExpectationKey(kind="check", ref=check_id))


class ChecksQueryProvider(QueryProvider):
    """Query provider serving what='checks'."""

    what: QueryWhat = "checks"

    def __init__(self, checks: ChecksStore) -> None:
        self._checks = checks

    async def query(self, project_id: str, q: MonitoringQuery) -> Page[CheckView]:
        all_checks = await self._checks.list(project_id)

        filtered = all_checks
        if q.id is not None:
            filtered = [c for c in filtered if c.id == q.id or c.check_id == q.id]
        if q.status is not None:
            filtered = [c for c in filtered if c.status == q.status]
        if q.subject is not None:
            filtered = [c for c in filtered if c.check_id == q.subject]

        total = len(filtered)
        items = filtered[q.offset : q.offset + q.limit]
        truncated = (q.offset + len(items)) < total
        return Page(rows=items, total=total, truncated=truncated)
