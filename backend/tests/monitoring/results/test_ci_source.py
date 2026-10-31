"""Tests for CiSourceReader (Task 4 - issue #460)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import insert

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.results.ci_source import CiSourceReader
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import (
    Cadence,
    CheckSpec,
    CiResults,
    FileResults,
    HandoffPayload,
    Operations,
)
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import projects
from mlops_agents.tracking.contract import CiRunRow, SourceType, SyncResult
from mlops_agents.tracking.service import TrackingService
from tests.conftest import MemoryKeyStore
from tests.monitoring.results.conftest import FakeHandoffs
from tests.proposals.samples import handoff as sample_handoff_dict
from tests.tracking.conftest import (
    FakeHandoffs as TrackingFakeHandoffs,
)
from tests.tracking.conftest import (
    FakeTrackingAdapter,
    TrackingEnv,
)
from tests.tracking.conftest import TestClock as ClockForTest


def _build_handoff_with_ci(check_id: str | None = None, workflow: str = "build") -> HandoffView:
    checks: list[CheckSpec] = []
    if check_id:
        checks.append(
            CheckSpec(
                check_id=check_id,
                name="Build Check",
                category="operational",
                cadence=Cadence(every_minutes=60, cron=None),
                timezone="UTC",
                results=CiResults(workflow=workflow),
            )
        )
    ops = Operations(
        scheduler=None,
        feeds=[],
        on_call=[],
        checks=checks,
        results_location=FileResults(connection="conn_a", path_pattern="*.jsonl"),
    )
    payload = HandoffPayload.model_validate(sample_handoff_dict(operations=ops.model_dump()))
    return HandoffView(
        proposal_id="prop_ci",
        version=1,
        approved=datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC),
        payload=payload,
    )


@pytest.fixture
async def tracking_env(
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
) -> TrackingEnv:
    project_id = "prj_citest"
    engine = await databases.app()
    async with engine.begin() as conn:
        await conn.execute(
            insert(projects).values(
                id=project_id,
                name="CI Test Project",
                source="github",
                github_full_name="org/demo",
                github_key="org/demo",
                default_branch="main",
                status="ok",
                created_at=datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC).isoformat(),
            )
        )
    await databases.project(project_id)
    clock = ClockForTest(datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC))
    secrets: Any = None  # TrackingService creates ProjectSecrets if keystore given
    events = SqliteEventLog(databases)
    adapter = FakeTrackingAdapter("github_actions")
    adapters: dict[SourceType, FakeTrackingAdapter] = {"github_actions": adapter}
    tracking_handoffs = TrackingFakeHandoffs()

    async def _async_gh_token() -> str | None:
        return "fake-token"

    service = TrackingService(
        databases=databases,
        keystore=keystore,
        secrets=secrets,
        events=events,
        adapters=adapters,
        github_token=_async_gh_token,
        clock=clock,
        handoffs=tracking_handoffs,
    )

    return TrackingEnv(
        service=service,
        databases=databases,
        keystore=keystore,
        secrets=secrets,
        events=events,
        adapters=adapters,
        project_id=project_id,
        clock=clock,  # type: ignore[arg-type]
        handoffs=tracking_handoffs,
    )


async def test_ci_failed_and_cancelled_runs_fire_succeeded_does_not(
    tracking_env: TrackingEnv,
) -> None:
    env = tracking_env
    # Create tracking source and sync runs
    source = await env.service.create_source(
        project_id=env.project_id,
        type="github_actions",
        settings={"repository": "org/demo", "workflows": ["build.yml"]},
        auth="none",
    )

    t1 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 30, 10, 10, 0, tzinfo=UTC)
    t3 = datetime(2026, 10, 30, 10, 20, 0, tzinfo=UTC)

    ci_runs = [
        CiRunRow(
            source_id=source.id,
            key="run_succeeded",
            workflow="build",
            status="succeeded",
            start=t1,
            duration_s=60.0,
            commit="c1",
            url="https://ci.example.com/1",
        ),
        CiRunRow(
            source_id=source.id,
            key="run_failed",
            workflow="build",
            status="failed",
            start=t2,
            duration_s=120.0,
            commit="c2",
            url="https://ci.example.com/2",
        ),
        CiRunRow(
            source_id=source.id,
            key="run_cancelled",
            workflow="build",
            status="cancelled",
            start=t3,
            duration_s=30.0,
            commit="c3",
            url="https://ci.example.com/3",
        ),
    ]

    env.adapters["github_actions"].result = SyncResult(
        versions=[], runs=[], metrics=[], aliases={}, ci_runs=ci_runs, evaluations=[], error=None
    )
    await env.service.run_sync(env.project_id, source.id)

    handoff = _build_handoff_with_ci(check_id="chk_build_wf", workflow="build")
    handoffs = FakeHandoffs(handoff)

    reader = CiSourceReader(tracking=env.service, handoffs=handoffs)
    outcome = await reader.read(env.project_id, "ci:build", cursor={})

    assert len(outcome.records) == 3
    assert outcome.consecutive_failures == 0
    assert outcome.last_error is None

    # Verify order and attributes
    k1, r1 = outcome.records[0]
    assert k1 == "run_succeeded"
    assert r1.check_id == "chk_build_wf"
    assert r1.fired is False
    assert r1.subject == "build"
    assert r1.category == "operational"
    assert r1.model is None
    assert r1.origin == "tracker"
    assert r1.source_ref == "https://ci.example.com/1"
    assert r1.ran_at == t1 + timedelta(seconds=60)
    assert r1.detail["status"] == "succeeded"
    assert r1.detail["commit"] == "c1"

    k2, r2 = outcome.records[1]
    assert k2 == "run_failed"
    assert r2.check_id == "chk_build_wf"
    assert r2.fired is True
    assert r2.ran_at == t2 + timedelta(seconds=120)
    assert r2.detail["status"] == "failed"

    k3, r3 = outcome.records[2]
    assert k3 == "run_cancelled"
    assert r3.check_id == "chk_build_wf"
    assert r3.fired is True
    assert r3.ran_at == t3 + timedelta(seconds=30)
    assert r3.detail["status"] == "cancelled"


async def test_ci_running_run_holds_cursor_until_it_ends(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="github_actions",
        settings={"repository": "org/demo", "workflows": ["test.yml"]},
        auth="none",
    )

    t1 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 30, 10, 5, 0, tzinfo=UTC)

    # First sync: run 1 is running
    run_1_running = CiRunRow(
        source_id=source.id,
        key="run_1",
        workflow="test",
        status="running",
        start=t1,
        duration_s=None,
        commit="c1",
        url="https://ci.example.com/run/1",
    )
    env.adapters["github_actions"].result = SyncResult(
        versions=[],
        runs=[],
        metrics=[],
        aliases={},
        ci_runs=[run_1_running],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    handoffs = FakeHandoffs(_build_handoff_with_ci(check_id="chk_test", workflow="test"))
    reader = CiSourceReader(tracking=env.service, handoffs=handoffs)

    outcome_1 = await reader.read(env.project_id, "ci:test", cursor={})
    # Running run is NOT read before it ends
    assert len(outcome_1.records) == 0
    # Cursor is held at run 1's start
    assert outcome_1.cursor.get("since") == t1.isoformat()

    # Second sync: run 1 has finished (failed), run 2 has succeeded
    run_1_finished = CiRunRow(
        source_id=source.id,
        key="run_1",
        workflow="test",
        status="failed",
        start=t1,
        duration_s=90.0,
        commit="c1",
        url="https://ci.example.com/run/1",
    )
    run_2_finished = CiRunRow(
        source_id=source.id,
        key="run_2",
        workflow="test",
        status="succeeded",
        start=t2,
        duration_s=45.0,
        commit="c2",
        url="https://ci.example.com/run/2",
    )
    env.adapters["github_actions"].result = SyncResult(
        versions=[],
        runs=[],
        metrics=[],
        aliases={},
        ci_runs=[run_1_finished, run_2_finished],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    outcome_2 = await reader.read(env.project_id, "ci:test", cursor=outcome_1.cursor)
    # Now both finished runs are read
    assert len(outcome_2.records) == 2
    assert outcome_2.records[0][0] == "run_1"
    assert outcome_2.records[0][1].fired is True
    assert outcome_2.records[1][0] == "run_2"
    assert outcome_2.records[1][1].fired is False

    # Third read: no new runs -> 0 records
    outcome_3 = await reader.read(env.project_id, "ci:test", cursor=outcome_2.cursor)
    assert len(outcome_3.records) == 0


async def test_ci_check_id_falls_back_to_source_id_when_not_in_handoff(
    tracking_env: TrackingEnv,
) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="github_actions",
        settings={"repository": "org/demo", "workflows": ["deploy.yml"]},
        auth="none",
    )
    t = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
    run = CiRunRow(
        source_id=source.id,
        key="run_d",
        workflow="deploy",
        status="succeeded",
        start=t,
        duration_s=10.0,
        commit="cd",
        url="https://ci.example.com/d",
    )
    env.adapters["github_actions"].result = SyncResult(
        versions=[], runs=[], metrics=[], aliases={}, ci_runs=[run], evaluations=[], error=None
    )
    await env.service.run_sync(env.project_id, source.id)

    # Handoff with no check naming "deploy"
    handoffs = FakeHandoffs(_build_handoff_with_ci(check_id=None, workflow="other"))
    reader = CiSourceReader(tracking=env.service, handoffs=handoffs)

    outcome = await reader.read(env.project_id, "ci:deploy", cursor={})
    assert len(outcome.records) == 1
    assert outcome.records[0][1].check_id == "ci:deploy"
