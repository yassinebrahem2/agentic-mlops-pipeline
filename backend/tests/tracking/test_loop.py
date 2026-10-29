"""Tests for the tracking sync loop (Task 4)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mlops_agents.errors import DomainError
from mlops_agents.integrations.github import api
from mlops_agents.storage.databases import Databases
from mlops_agents.tracking.constants import (
    DEFAULT_SYNC_INTERVAL_S,
)
from mlops_agents.tracking.contract import TrackingQuery, TrackingSourceView
from mlops_agents.tracking.loop import TrackingLoop
from mlops_agents.tracking.service import TrackingService
from tests.api.conftest import LOCAL_URL, add_local_project, signed_in, started_app
from tests.fakes.fake_github import FakeGitHubState, start_fake_github
from tests.fakes.fake_mlflow import FakeMlflowState, start_fake_mlflow
from tests.tracking.conftest import FakeHandoffs


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class RecordingTrackingDouble:
    def __init__(self) -> None:
        self.projects_sources: dict[str, list[TrackingSourceView]] = {}
        self.intervals: dict[str, int] = {}
        self.sync_calls: list[tuple[str, str | None]] = []
        self.fail_sync_sources: set[str] = set()
        self.fail_projects: set[str] = set()
        self.sync_started_event: asyncio.Event | None = None
        self.sync_block_event: asyncio.Event | None = None

    async def sources(self, project_id: str) -> list[TrackingSourceView]:
        if project_id in self.fail_projects:
            raise RuntimeError(f"Database error for project {project_id}")
        return self.projects_sources.get(project_id, [])

    async def sync_interval(self, project_id: str) -> int:
        return self.intervals.get(project_id, DEFAULT_SYNC_INTERVAL_S)

    async def run_sync(self, project_id: str, source_id: str | None = None) -> None:
        self.sync_calls.append((project_id, source_id))
        if self.sync_started_event:
            self.sync_started_event.set()
        if self.sync_block_event:
            await self.sync_block_event.wait()
        if source_id in self.fail_sync_sources:
            raise RuntimeError(f"Sync failed for {source_id}")


def _make_source(
    source_id: str = "trk_1",
    last_sync: datetime | None = None,
    last_error: str | None = None,
) -> TrackingSourceView:
    return TrackingSourceView(
        id=source_id,
        type="mlflow",
        settings={"url": "http://mlflow.example.com"},
        auth="none",
        credential_set=False,
        last_sync=last_sync,
        last_error=last_error,
    )


# 1. A source never synced is synced on the first pass
async def test_loop_syncs_never_synced_source_on_first_pass() -> None:
    clock = FakeClock()
    double = RecordingTrackingDouble()
    double.projects_sources["prj_1"] = [_make_source("trk_1", last_sync=None)]

    loop = TrackingLoop(
        tracking=double,
        projects=lambda: _async_projects(["prj_1"]),
        clock=clock,
        sleep=_no_sleep,
    )
    await loop.run_pass()

    assert double.sync_calls == [("prj_1", "trk_1")]


# 2. A source synced less than its interval ago is skipped, and synced when the clock passes it
async def test_loop_skips_recent_source_and_syncs_when_due() -> None:
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(t0)
    double = RecordingTrackingDouble()
    double.intervals["prj_1"] = 60
    # Synced 10 seconds ago
    double.projects_sources["prj_1"] = [_make_source("trk_1", last_sync=t0 - timedelta(seconds=10))]

    loop = TrackingLoop(
        tracking=double,
        projects=lambda: _async_projects(["prj_1"]),
        clock=clock,
        sleep=_no_sleep,
    )

    # Pass at t0 (10s since sync < 60s) -> skipped
    await loop.run_pass()
    assert len(double.sync_calls) == 0

    # Advance clock to t0 + 55s (65s since sync >= 60s) -> due!
    clock.advance(55)
    await loop.run_pass()
    assert double.sync_calls == [("prj_1", "trk_1")]


# 3. Changing the interval takes effect on the next pass
async def test_loop_changing_interval_takes_effect_on_next_pass() -> None:
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(t0)
    double = RecordingTrackingDouble()
    double.intervals["prj_1"] = 60
    # Synced at t0
    double.projects_sources["prj_1"] = [_make_source("trk_1", last_sync=t0)]

    loop = TrackingLoop(
        tracking=double,
        projects=lambda: _async_projects(["prj_1"]),
        clock=clock,
        sleep=_no_sleep,
    )

    # At t0 + 20s with 60s interval -> skipped
    clock.advance(20)
    await loop.run_pass()
    assert len(double.sync_calls) == 0

    # Change interval to 15s (20s >= 15s) -> next pass syncs
    double.intervals["prj_1"] = 15
    await loop.run_pass()
    assert double.sync_calls == [("prj_1", "trk_1")]


# 4. A source in error is retried once per interval, not each tick
async def test_loop_retries_error_source_once_per_interval_not_each_tick() -> None:
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(t0)
    double = RecordingTrackingDouble()
    double.intervals["prj_1"] = 60
    double.fail_sync_sources.add("trk_err")
    double.projects_sources["prj_1"] = [_make_source("trk_err", last_sync=None)]

    loop = TrackingLoop(
        tracking=double,
        projects=lambda: _async_projects(["prj_1"]),
        clock=clock,
        sleep=_no_sleep,
    )

    # First pass: attempted, fails.
    await loop.run_pass()
    assert double.sync_calls == [("prj_1", "trk_err")]

    # Tick 1: 15s later (< 60s) -> skipped
    clock.advance(15)
    await loop.run_pass()
    assert len(double.sync_calls) == 1

    # Tick 2: 30s later (< 60s) -> skipped
    clock.advance(15)
    await loop.run_pass()
    assert len(double.sync_calls) == 1

    # Tick 4: 60s total (>= 60s) -> retried!
    clock.advance(30)
    await loop.run_pass()
    assert len(double.sync_calls) == 2


# 5. A project without sources and a removed project cost no call
async def test_loop_project_without_sources_and_removed_project_cost_no_call() -> None:
    double = RecordingTrackingDouble()
    double.projects_sources["prj_empty"] = []
    double.fail_projects.add("prj_removed")

    loop = TrackingLoop(
        tracking=double,
        projects=lambda: _async_projects(["prj_empty", "prj_removed"]),
        clock=FakeClock(),
        sleep=_no_sleep,
    )
    await loop.run_pass()

    assert len(double.sync_calls) == 0


# 6. An exception in one project does not stop the others or the loop
async def test_loop_exception_in_one_project_does_not_stop_others() -> None:
    double = RecordingTrackingDouble()
    double.fail_projects.add("prj_broken")
    double.projects_sources["prj_good"] = [_make_source("trk_good", last_sync=None)]

    loop = TrackingLoop(
        tracking=double,
        projects=lambda: _async_projects(["prj_broken", "prj_good"]),
        clock=FakeClock(),
        sleep=_no_sleep,
    )
    await loop.run_pass()

    assert double.sync_calls == [("prj_good", "trk_good")]


# 7. Close ends the loop during a sleep and during a sync
async def test_loop_close_during_sleep() -> None:
    double = RecordingTrackingDouble()
    sleep_started = asyncio.Event()

    async def blocking_sleep(duration: float) -> None:
        sleep_started.set()
        await asyncio.sleep(duration)

    loop = TrackingLoop(
        tracking=double,
        projects=lambda: _async_projects([]),
        clock=FakeClock(),
        sleep=blocking_sleep,
        tick_s=100.0,
    )
    task = await loop.start()
    await sleep_started.wait()

    await loop.close()
    assert task.done()


async def test_loop_close_during_sync() -> None:
    double = RecordingTrackingDouble()
    double.projects_sources["prj_1"] = [_make_source("trk_1", last_sync=None)]
    sync_started = asyncio.Event()
    sync_blocked = asyncio.Event()
    double.sync_started_event = sync_started
    double.sync_block_event = sync_blocked

    loop = TrackingLoop(
        tracking=double,
        projects=lambda: _async_projects(["prj_1"]),
        clock=FakeClock(),
        sleep=_no_sleep,
    )
    task = asyncio.create_task(loop.run_pass())
    await sync_started.wait()

    # Cancel while waiting in run_sync
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# 8. With the real service and both fakes, a loop started on a temp app fills the record and records tracking.synced
async def test_loop_with_real_service_fills_record_and_records_event(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    fake_mlflow_state = FakeMlflowState()
    fake_mlflow_state.add_experiment("Default", experiment_id="exp_1")
    fake_mlflow_state.add_run(
        experiment_id="exp_1",
        run_id="run_1",
        status="FINISHED",
    )
    fake_mlflow_state.log_metric(run_id="run_1", key="loss", value=0.42)
    running_mlflow = start_fake_mlflow(fake_mlflow_state)

    fake_gh_state = FakeGitHubState()
    fake_gh_state.workflow_runs["org/demo"] = [
        {
            "id": 101,
            "name": "CI",
            "path": ".github/workflows/ci.yml",
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-10-28T12:00:00Z",
            "run_started_at": "2026-10-28T12:00:00Z",
            "updated_at": "2026-10-28T12:05:00Z",
            "head_sha": "abc1234",
            "html_url": "https://github.com/org/demo/actions/runs/101",
        }
    ]
    running_gh = start_fake_github(fake_gh_state)
    original_api = api.GITHUB_API
    api.GITHUB_API = running_gh.base_url

    try:
        async with started_app(tmp_path / "data") as app:
            project_id = await add_local_project(app, tmp_path, git_env)
            services = app.state.services

            # Set GitHub token through the public GitHub service
            await services.github.set_token("fake-gh-token")

            # Set short sync interval through existing tracking settings
            await services.tracking.set_sync_interval(project_id, 15)

            # Create both sources pointing to the real running fakes
            source_mlflow = await services.tracking.create_source(
                project_id=project_id,
                type="mlflow",
                settings={"url": running_mlflow.base_url},
                auth="none",
            )
            source_gh = await services.tracking.create_source(
                project_id=project_id,
                type="github_actions",
                settings={"repository": "org/demo"},
                auth="none",
            )

            # Wait for the app's started tracking loop to sync both sources on its own
            async with asyncio.timeout(30):
                while True:
                    events = await services.events.history(project_id, types=["tracking.synced"])
                    synced_source_ids = {
                        e.payload["source_id"] for e in events if e.payload.get("ok")
                    }
                    if source_mlflow.id in synced_source_ids and source_gh.id in synced_source_ids:
                        break
                    await asyncio.sleep(0.5)

            # Check MLflow runs are synced
            q_runs = await services.tracking.query(
                project_id,
                TrackingQuery(what="runs"),
            )
            assert len(q_runs.rows) >= 1
            assert q_runs.rows[0].key == "run_1"

            # Check GitHub CI runs are synced
            q_ci = await services.tracking.query(
                project_id,
                TrackingQuery(what="ci_runs"),
            )
            assert len(q_ci.rows) >= 1
            assert q_ci.rows[0].workflow == "ci.yml"

            # Check tracking.synced event was recorded for both sources
            events = await services.events.history(project_id, types=["tracking.synced"])
            synced_source_ids = {e.payload["source_id"] for e in events if e.payload.get("ok")}
            assert source_mlflow.id in synced_source_ids
            assert source_gh.id in synced_source_ids
    finally:
        api.GITHUB_API = original_api
        running_gh.stop()
        running_mlflow.stop()


async def test_loop_start_returns_at_once_with_blackholed_source() -> None:
    double = RecordingTrackingDouble()
    double.projects_sources["prj_1"] = [_make_source("trk_blackhole", last_sync=None)]
    unblock = asyncio.Event()

    async def hanging_sleep(_: float) -> None:
        pass

    double.sync_block_event = unblock

    loop = TrackingLoop(
        tracking=double,
        projects=lambda: _async_projects(["prj_1"]),
        clock=FakeClock(),
        sleep=hanging_sleep,
    )
    # start() must return immediately without waiting for the blackholed run_sync
    task = await asyncio.wait_for(loop.start(), timeout=0.5)
    assert not task.done()
    # Cleanup
    unblock.set()
    await loop.close()


async def test_loop_survives_failing_projects() -> None:
    double = RecordingTrackingDouble()

    async def bad_projects() -> list[str]:
        raise RuntimeError("database unreachable")

    loop = TrackingLoop(
        tracking=double,
        projects=bad_projects,
        clock=FakeClock(),
        sleep=_no_sleep,
    )
    # Must not raise
    await loop.run_pass()
    assert len(double.sync_calls) == 0


async def test_loop_failing_source_next_to_healthy_source_in_same_project() -> None:
    double = RecordingTrackingDouble()
    double.projects_sources["prj_1"] = [
        _make_source("trk_fail", last_sync=None),
        _make_source("trk_healthy", last_sync=None),
    ]
    double.fail_sync_sources.add("trk_fail")

    loop = TrackingLoop(
        tracking=double,
        projects=lambda: _async_projects(["prj_1"]),
        clock=FakeClock(),
        sleep=_no_sleep,
    )
    await loop.run_pass()

    assert ("prj_1", "trk_fail") in double.sync_calls
    assert ("prj_1", "trk_healthy") in double.sync_calls


# 9. The settings route round-trips and refuses an interval below the minimum
async def test_settings_route_roundtrips_and_refuses_below_minimum(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    async with started_app(tmp_path / "data") as app:
        project_id = await add_local_project(app, tmp_path, git_env)
        async with signed_in(app, LOCAL_URL) as client:
            # 1. GET default settings
            res = await client.get(f"/api/projects/{project_id}/tracking/settings")
            assert res.status_code == 200
            assert res.json() == {"sync_interval_s": DEFAULT_SYNC_INTERVAL_S}

            # 2. PUT valid settings
            res = await client.put(
                f"/api/projects/{project_id}/tracking/settings",
                json={"sync_interval_s": 30},
            )
            assert res.status_code == 200
            assert res.json() == {"sync_interval_s": 30}

            # Verify GET reflects update
            res = await client.get(f"/api/projects/{project_id}/tracking/settings")
            assert res.status_code == 200
            assert res.json() == {"sync_interval_s": 30}

            # 3. PUT below minimum (MIN_SYNC_INTERVAL_S is 15) -> 422
            res = await client.put(
                f"/api/projects/{project_id}/tracking/settings",
                json={"sync_interval_s": 5},
            )
            assert res.status_code == 422


# Unit test for TrackingService.sync_interval and set_sync_interval
async def test_tracking_service_sync_interval(tmp_path: Path) -> None:
    from mlops_agents.credentials.project_secrets import ProjectSecrets
    from mlops_agents.events.log import SqliteEventLog
    from mlops_agents.paths import DataPaths
    from tests.conftest import MemoryKeyStore

    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    databases = Databases(paths)
    await databases.app()

    # Create project in app.db
    from sqlalchemy import insert

    from mlops_agents.storage.tables import projects

    engine = await databases.app()
    async with engine.begin() as conn:
        await conn.execute(
            insert(projects).values(
                id="prj_test",
                name="Test Project",
                source="github",
                github_full_name="org/demo",
                github_key="org/demo",
                default_branch="main",
                status="ok",
                created_at="2026-10-28T12:00:00Z",
            )
        )
    await databases.project("prj_test")

    keystore = MemoryKeyStore()
    secrets = ProjectSecrets(keystore, databases)
    events = SqliteEventLog(databases)

    async def _dummy_token() -> str | None:
        return None

    service = TrackingService(
        databases=databases,
        keystore=keystore,
        secrets=secrets,
        events=events,
        adapters={},
        github_token=_dummy_token,
        handoffs=FakeHandoffs(),
    )

    # Default
    assert await service.sync_interval("prj_test") == DEFAULT_SYNC_INTERVAL_S

    # Set
    updated = await service.set_sync_interval("prj_test", 45)
    assert updated == 45
    assert await service.sync_interval("prj_test") == 45

    # Refuse below minimum
    with pytest.raises(DomainError) as exc_info:
        await service.set_sync_interval("prj_test", 10)
    assert exc_info.value.status == 422


async def _async_projects(items: list[str]) -> list[str]:
    return items


async def _no_sleep(_: float) -> None:
    pass
