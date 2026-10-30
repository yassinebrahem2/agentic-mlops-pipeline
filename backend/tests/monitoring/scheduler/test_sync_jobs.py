"""Tests for sync_tracking, sync_prs, and after_sync scheduled jobs (Task 3)."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from mlops_agents.monitoring.jobs import JobDue, JobKind
from mlops_agents.monitoring.scheduler.loop import AppScheduler
from mlops_agents.monitoring.scheduler.sync_jobs import (
    AfterSyncJob,
    SyncPrsJob,
    SyncTrackingJob,
)
from mlops_agents.monitoring.scheduler.times import interval_dues
from mlops_agents.monitoring.settings import set_monitoring_settings
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import scheduler_jobs
from tests.providers.fake_clock import FakeClock


class RecordingTrackingService:
    def __init__(self, interval: int = 60) -> None:
        self.interval = interval
        self.sync_calls: list[str] = []

    async def sync_interval(self, project_id: str) -> int:
        return self.interval

    async def run_sync(
        self, project_id: str, source_id: str | None = None, *, check_project: bool = True
    ) -> None:
        self.sync_calls.append(project_id)


class RecordingPrSync:
    def __init__(self) -> None:
        self.sync_calls: list[str] = []

    async def sync_now(self, project_id: str) -> None:
        self.sync_calls.append(project_id)


class RecordingCollectJob:
    kind = JobKind.COLLECT_RESULTS
    interval_s = 60

    def __init__(self, execution_order: list[str]) -> None:
        self.execution_order = execution_order

    async def due(self, project_id: str, after: datetime, now: datetime) -> list[JobDue]:
        points = interval_dues(self.interval_s, after, now)
        return [JobDue(kind=self.kind, due=p, subject="") for p in points]

    async def run(self, project_id: str, job: JobDue) -> None:
        self.execution_order.append(f"collect_results:{project_id}")


class RecordingSyncObserver:
    def __init__(self, name: str, execution_order: list[str] | None = None) -> None:
        self.name = name
        self.execution_order = execution_order
        self.calls: list[tuple[str, datetime]] = []

    async def synced(self, project_id: str, now: datetime) -> None:
        self.calls.append((project_id, now))
        if self.execution_order is not None:
            self.execution_order.append(f"sync_observer:{self.name}:{project_id}")


class FailingSyncObserver:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls: list[tuple[str, datetime]] = []

    async def synced(self, project_id: str, now: datetime) -> None:
        self.calls.append((project_id, now))
        raise self.exc


@pytest.fixture
def project_id() -> str:
    return "prj_syncjobs"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC))


async def test_sync_tracking_job_due_and_run(project_id: str, clock: FakeClock) -> None:
    tracking = RecordingTrackingService(interval=120)
    job = SyncTrackingJob(tracking)
    assert job.kind == JobKind.SYNC_TRACKING

    t0 = clock.now()
    t1 = t0 + timedelta(seconds=240)
    dues = await job.due(project_id, t0, t1)
    assert len(dues) == 1
    assert dues[0].kind == JobKind.SYNC_TRACKING
    assert dues[0].due == t1

    await job.run(project_id, dues[0])
    assert tracking.sync_calls == [project_id]


async def test_sync_prs_job_due_and_run(
    databases: Databases, project_id: str, clock: FakeClock
) -> None:
    await set_monitoring_settings(databases, project_id, {"collection_interval_s": 30})
    pr_sync = RecordingPrSync()
    job = SyncPrsJob(databases, pr_sync)
    assert job.kind == JobKind.SYNC_PRS

    t0 = clock.now()
    t1 = t0 + timedelta(seconds=60)
    dues = await job.due(project_id, t0, t1)
    assert len(dues) == 1
    assert dues[0].kind == JobKind.SYNC_PRS
    assert dues[0].due == t1

    await job.run(project_id, dues[0])
    assert pr_sync.sync_calls == [project_id]


async def test_after_sync_job_runs_observers_in_registration_order(
    databases: Databases, project_id: str, clock: FakeClock
) -> None:
    order: list[str] = []
    obs1 = RecordingSyncObserver("first", order)
    obs2 = RecordingSyncObserver("second", order)

    job = AfterSyncJob(databases, [obs1, obs2], clock=clock)
    assert job.kind == JobKind.AFTER_SYNC

    t0 = clock.now()
    t1 = t0 + timedelta(seconds=60)
    dues = await job.due(project_id, t0, t1)
    assert len(dues) == 1

    await job.run(project_id, dues[0])
    assert order == [f"sync_observer:first:{project_id}", f"sync_observer:second:{project_id}"]
    assert len(obs1.calls) == 1
    assert len(obs2.calls) == 1


async def test_catching_up_runs_sync_tracking_sync_prs_collect_after_sync_in_order(
    databases: Databases, project_id: str, clock: FakeClock
) -> None:
    execution_order: list[str] = []

    tracking = RecordingTrackingService(interval=60)
    orig_run_sync = tracking.run_sync

    async def tracked_run_sync(
        p_id: str, source_id: str | None = None, *, check_project: bool = True
    ) -> None:
        execution_order.append(f"sync_tracking:{p_id}")
        await orig_run_sync(p_id, source_id, check_project=check_project)

    tracking.run_sync = tracked_run_sync  # type: ignore[assignment]

    pr_sync = RecordingPrSync()
    orig_sync_now = pr_sync.sync_now

    async def tracked_sync_now(p_id: str) -> None:
        execution_order.append(f"sync_prs:{p_id}")
        await orig_sync_now(p_id)

    pr_sync.sync_now = tracked_sync_now  # type: ignore[assignment]

    collect_job = RecordingCollectJob(execution_order)

    scheduler = AppScheduler(
        databases=databases,
        projects=lambda: [project_id],  # type: ignore[return-value]
        clock=clock,
        sleep=clock.sleep,
    )

    sync_tracking_job = SyncTrackingJob(tracking)
    sync_prs_job = SyncPrsJob(databases, pr_sync)
    after_sync_job = AfterSyncJob(databases, scheduler, clock=clock)

    obs = RecordingSyncObserver("main", execution_order)
    scheduler.add_sync_observer(obs)

    scheduler.register(sync_tracking_job)
    scheduler.register(sync_prs_job)
    scheduler.register(collect_job)
    scheduler.register(after_sync_job)

    # Establish baseline
    t0 = clock.now()
    await scheduler.run_due(project_id, t0)
    assert execution_order == [
        f"sync_tracking:{project_id}",
        f"sync_prs:{project_id}",
        f"collect_results:{project_id}",
        f"sync_observer:main:{project_id}",
    ]

    execution_order.clear()

    # Advance clock to trigger catch-up
    clock.advance(3600)
    await scheduler.run_due(project_id, clock.now())
    assert execution_order == [
        f"sync_tracking:{project_id}",
        f"sync_prs:{project_id}",
        f"collect_results:{project_id}",
        f"sync_observer:main:{project_id}",
    ]


async def test_raising_sync_observer_does_not_stop_next_and_error_recorded_on_job_row(
    databases: Databases, project_id: str, clock: FakeClock
) -> None:
    failing_obs = FailingSyncObserver(
        ValueError("observer failed: https://token:sensitive_pass@api")
    )
    order: list[str] = []
    succeeding_obs = RecordingSyncObserver("second", order)

    scheduler = AppScheduler(
        databases=databases,
        projects=lambda: [project_id],  # type: ignore[return-value]
        clock=clock,
        sleep=clock.sleep,
    )
    scheduler.add_sync_observer(failing_obs)
    scheduler.add_sync_observer(succeeding_obs)

    after_sync_job = AfterSyncJob(databases, scheduler, clock=clock)
    scheduler.register(after_sync_job)

    now = clock.now()
    await scheduler.run_due(project_id, now)

    # Observer 2 still ran!
    assert len(succeeding_obs.calls) == 1
    assert order == [f"sync_observer:second:{project_id}"]

    # Error is recorded on the job row, finished is NULL, secret is scrubbed
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(scheduler_jobs.c.error, scheduler_jobs.c.finished).where(
                    scheduler_jobs.c.kind == JobKind.AFTER_SYNC.value
                )
            )
        ).first()

    assert row is not None
    assert row.finished is None
    assert row.error is not None
    assert "observer failed" in row.error
    assert "sensitive_pass" not in row.error
    assert "***@" in row.error


def test_no_tracking_loop_left() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("mlops_agents.tracking.loop")

    from mlops_agents.api.services import Services

    assert "tracking_loop" not in Services.__dataclass_fields__
