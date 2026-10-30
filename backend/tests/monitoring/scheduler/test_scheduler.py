"""Tests for AppScheduler, store, and execution order (I24)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.clock import to_iso
from mlops_agents.monitoring.jobs import JobDue, JobKind
from mlops_agents.monitoring.scheduler.loop import AppScheduler
from mlops_agents.monitoring.scheduler.times import interval_dues
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import scheduler_jobs
from tests.providers.fake_clock import FakeClock


class FakeJob:
    """A fake scheduled job recording its runs and returning configured dues."""

    def __init__(
        self,
        kind: JobKind,
        interval_s: int = 60,
        dues_to_return: list[JobDue] | None = None,
        should_raise: Exception | None = None,
    ) -> None:
        self.kind = kind
        self.interval_s = interval_s
        self._dues_to_return = dues_to_return
        self._should_raise = should_raise
        self.runs: list[tuple[str, JobDue]] = []

    async def due(self, project_id: str, after: datetime, now: datetime) -> list[JobDue]:
        if self._dues_to_return is not None:
            return self._dues_to_return
        points = interval_dues(self.interval_s, after, now)
        return [JobDue(kind=self.kind, due=p, subject="") for p in points]

    async def run(self, project_id: str, job: JobDue) -> None:
        if self._should_raise is not None:
            raise self._should_raise
        self.runs.append((project_id, job))


class FakeSyncObserver:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, datetime]] = []

    async def synced(self, project_id: str, now: datetime) -> None:
        self.calls.append((project_id, now))


async def _async_projects(project_id: str) -> list[str]:
    return [project_id]


async def _empty_projects() -> list[str]:
    return []


@pytest.fixture
def project_id() -> str:
    return "prj_testscheduler"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC))


async def test_jobs_run_in_job_order_then_due_time(
    databases: Databases, project_id: str, clock: FakeClock
) -> None:
    # SYNC_TRACKING comes before CHECK_EXPECTATIONS in JOB_ORDER
    t1 = clock.now() + timedelta(seconds=10)
    t2 = clock.now() + timedelta(seconds=20)
    t3 = clock.now() + timedelta(seconds=30)

    # Job A is CHECK_EXPECTATIONS (later in JOB_ORDER) with earlier and later dues
    job_a = FakeJob(
        JobKind.CHECK_EXPECTATIONS,
        dues_to_return=[
            JobDue(kind=JobKind.CHECK_EXPECTATIONS, due=t1, subject="a1"),
            JobDue(kind=JobKind.CHECK_EXPECTATIONS, due=t3, subject="a3"),
        ],
    )
    # Job B is SYNC_TRACKING (earlier in JOB_ORDER) with interleaved dues
    job_b = FakeJob(
        JobKind.SYNC_TRACKING,
        dues_to_return=[
            JobDue(kind=JobKind.SYNC_TRACKING, due=t2, subject="b2"),
            JobDue(kind=JobKind.SYNC_TRACKING, due=t1, subject="b1"),
        ],
    )

    scheduler = AppScheduler(
        databases=databases,
        projects=lambda: _async_projects(project_id),
        clock=clock,
        sleep=asyncio.sleep,
    )
    scheduler.register(job_a)
    scheduler.register(job_b)

    all_runs: list[JobDue] = []

    async def run_tracker(project: str, j: JobDue) -> None:
        all_runs.append(j)

    job_a.run = run_tracker  # type: ignore[assignment]
    job_b.run = run_tracker  # type: ignore[assignment]

    await scheduler.run_due(project_id, clock.now() + timedelta(seconds=60))

    # Expect: all SYNC_TRACKING first (sorted by due: b1 at t1, then b2 at t2),
    # then CHECK_EXPECTATIONS (sorted by due: a1 at t1, then a3 at t3).
    assert len(all_runs) == 4
    assert [j.subject for j in all_runs] == ["b1", "b2", "a1", "a3"]


async def test_missed_batch_after_gap_runs_each_interval_once(
    databases: Databases, project_id: str, clock: FakeClock
) -> None:
    job1 = FakeJob(JobKind.SYNC_TRACKING, interval_s=60)
    job2 = FakeJob(JobKind.COLLECT_RESULTS, interval_s=60)

    scheduler = AppScheduler(
        databases=databases,
        projects=lambda: _async_projects(project_id),
        clock=clock,
        sleep=asyncio.sleep,
    )
    scheduler.register(job1)
    scheduler.register(job2)

    # Initial run establishes the cursor
    t0 = clock.now()
    await scheduler.run_due(project_id, t0)
    assert len(job1.runs) == 1
    assert len(job2.runs) == 1

    # Gap of 5 hours:
    clock.advance(5 * 3600)
    t_after_gap = clock.now()

    await scheduler.run_due(project_id, t_after_gap)

    # Each interval job must run exactly ONCE more (latest grid point)
    assert len(job1.runs) == 2
    assert len(job2.runs) == 2


async def test_raising_job_is_recorded_and_next_still_runs(
    databases: Databases, project_id: str, clock: FakeClock
) -> None:
    failing_job = FakeJob(
        JobKind.SYNC_TRACKING,
        dues_to_return=[JobDue(kind=JobKind.SYNC_TRACKING, due=clock.now(), subject="failing")],
        should_raise=RuntimeError("connection failed to https://user:secret_pass@example.com/api"),
    )
    succeeding_job = FakeJob(
        JobKind.COLLECT_RESULTS,
        dues_to_return=[JobDue(kind=JobKind.COLLECT_RESULTS, due=clock.now(), subject="ok")],
    )

    scheduler = AppScheduler(
        databases=databases,
        projects=lambda: _async_projects(project_id),
        clock=clock,
        sleep=asyncio.sleep,
    )
    scheduler.register(failing_job)
    scheduler.register(succeeding_job)

    await scheduler.run_due(project_id, clock.now())

    # Succeeding job ran
    assert len(succeeding_job.runs) == 1

    # Check database: failing job row has scrubbed error recorded, finished is NULL
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(scheduler_jobs.c.kind, scheduler_jobs.c.finished, scheduler_jobs.c.error)
            )
        ).all()

    row_map = {r.kind: r for r in rows}
    assert row_map[JobKind.SYNC_TRACKING.value].finished is None
    err = row_map[JobKind.SYNC_TRACKING.value].error
    assert err is not None
    assert "secret_pass" not in err
    assert "***@" in err

    assert row_map[JobKind.COLLECT_RESULTS.value].finished is not None
    assert row_map[JobKind.COLLECT_RESULTS.value].error is None


async def test_row_left_started_is_run_again_finished_is_not(
    databases: Databases, project_id: str, clock: FakeClock
) -> None:
    now = clock.now()
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        # 1. Started row (crashed before finish): finished=NULL, error=NULL
        await conn.execute(
            sqlite_insert(scheduler_jobs).values(
                kind=JobKind.SYNC_TRACKING.value,
                due=to_iso(now - timedelta(seconds=120)),
                subject="crashed",
                started=to_iso(now - timedelta(seconds=110)),
                finished=None,
                error=None,
            )
        )
        # 2. Finished row: finished set
        await conn.execute(
            sqlite_insert(scheduler_jobs).values(
                kind=JobKind.SYNC_TRACKING.value,
                due=to_iso(now - timedelta(seconds=60)),
                subject="already_finished",
                started=to_iso(now - timedelta(seconds=55)),
                finished=to_iso(now - timedelta(seconds=50)),
                error=None,
            )
        )

    job = FakeJob(JobKind.SYNC_TRACKING, dues_to_return=[])
    scheduler = AppScheduler(
        databases=databases,
        projects=lambda: _async_projects(project_id),
        clock=clock,
        sleep=asyncio.sleep,
    )
    scheduler.register(job)

    await scheduler.run_due(project_id, now)

    # Only the crashed row was run again
    assert len(job.runs) == 1
    assert job.runs[0][1].subject == "crashed"

    # Verify that crashed row is now finished
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(scheduler_jobs.c.subject, scheduler_jobs.c.finished).where(
                    scheduler_jobs.c.subject == "crashed"
                )
            )
        ).all()
    assert rows[0].finished is not None


async def test_two_run_due_calls_in_a_row_run_nothing_twice(
    databases: Databases, project_id: str, clock: FakeClock
) -> None:
    job = FakeJob(JobKind.SYNC_TRACKING, interval_s=60)
    scheduler = AppScheduler(
        databases=databases,
        projects=lambda: _async_projects(project_id),
        clock=clock,
        sleep=asyncio.sleep,
    )
    scheduler.register(job)

    now = clock.now()
    await scheduler.run_due(project_id, now)
    assert len(job.runs) == 1

    # Second call immediately with same time: nothing runs
    await scheduler.run_due(project_id, now)
    assert len(job.runs) == 1


async def test_scheduler_sync_observers_registration_order(
    databases: Databases, clock: FakeClock
) -> None:
    scheduler = AppScheduler(
        databases=databases,
        projects=_empty_projects,
        clock=clock,
        sleep=asyncio.sleep,
    )
    obs1 = FakeSyncObserver("first")
    obs2 = FakeSyncObserver("second")
    scheduler.add_sync_observer(obs1)
    scheduler.add_sync_observer(obs2)

    assert scheduler.sync_observers == (obs1, obs2)


async def test_scheduler_start_and_stop(
    databases: Databases, project_id: str, clock: FakeClock
) -> None:
    job = FakeJob(JobKind.SYNC_TRACKING, interval_s=5)
    scheduler = AppScheduler(
        databases=databases,
        projects=lambda: _async_projects(project_id),
        clock=clock,
        sleep=asyncio.sleep,
    )
    scheduler.register(job)

    await scheduler.start()
    # Advance clock to trigger ticks
    clock.advance(10)
    # Give task a moment to execute
    await asyncio.sleep(0.01)

    await scheduler.stop()
    assert scheduler.task is None
