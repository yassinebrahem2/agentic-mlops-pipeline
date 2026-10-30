"""Scheduler database store for scheduler_jobs table."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.clock import to_iso
from mlops_agents.monitoring.jobs import JOB_ORDER, JobDue, JobKind
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import scheduler_jobs


def _parse_due(val: str) -> datetime:
    dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class SchedulerStore:
    """Operations on the project's scheduler_jobs table."""

    def __init__(self, databases: Databases) -> None:
        self._databases = databases

    async def latest_due(self, project_id: str, kind: JobKind) -> datetime | None:
        """Find the latest due datetime already stored for this job kind."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    select(scheduler_jobs.c.due)
                    .where(scheduler_jobs.c.kind == kind.value)
                    .order_by(scheduler_jobs.c.due.desc())
                    .limit(1)
                )
            ).first()
        if row is None or row.due is None:
            return None
        return _parse_due(row.due)

    async def insert_dues(self, project_id: str, dues: list[JobDue]) -> None:
        """Insert dues into scheduler_jobs. Existing (kind, due, subject) rows are skipped."""
        if not dues:
            return
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            for job in dues:
                stmt = (
                    sqlite_insert(scheduler_jobs)
                    .values(
                        kind=job.kind.value,
                        due=to_iso(job.due),
                        subject=job.subject,
                    )
                    .on_conflict_do_nothing()
                )
                await conn.execute(stmt)

    async def unfinished_jobs(self, project_id: str) -> list[JobDue]:
        """Returns unfinished jobs in JOB_ORDER, then by due time.

        Unfinished rows have finished IS NULL and error IS NULL.
        """
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(
                        scheduler_jobs.c.kind,
                        scheduler_jobs.c.due,
                        scheduler_jobs.c.subject,
                    ).where(
                        scheduler_jobs.c.finished.is_(None),
                        scheduler_jobs.c.error.is_(None),
                    )
                )
            ).all()

        parsed: list[JobDue] = []
        for r in rows:
            try:
                kind = JobKind(r.kind)
            except ValueError:
                continue
            due_dt = _parse_due(r.due)
            parsed.append(JobDue(kind=kind, due=due_dt, subject=r.subject))

        # Sort in JOB_ORDER, then by due time
        order_index = {kind: i for i, kind in enumerate(JOB_ORDER)}
        parsed.sort(key=lambda j: (order_index.get(j.kind, 999), j.due))
        return parsed

    async def mark_started(self, project_id: str, job: JobDue, started_at: datetime) -> None:
        """Record when a job starts execution."""
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                update(scheduler_jobs)
                .where(
                    scheduler_jobs.c.kind == job.kind.value,
                    scheduler_jobs.c.due == to_iso(job.due),
                    scheduler_jobs.c.subject == job.subject,
                )
                .values(started=to_iso(started_at))
            )

    async def mark_finished(self, project_id: str, job: JobDue, finished_at: datetime) -> None:
        """Record when a job successfully finishes execution."""
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                update(scheduler_jobs)
                .where(
                    scheduler_jobs.c.kind == job.kind.value,
                    scheduler_jobs.c.due == to_iso(job.due),
                    scheduler_jobs.c.subject == job.subject,
                )
                .values(finished=to_iso(finished_at))
            )

    async def mark_error(self, project_id: str, job: JobDue, error: str) -> None:
        """Record the scrubbed error when a job raises."""
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                update(scheduler_jobs)
                .where(
                    scheduler_jobs.c.kind == job.kind.value,
                    scheduler_jobs.c.due == to_iso(job.due),
                    scheduler_jobs.c.subject == job.subject,
                )
                .values(error=error)
            )
