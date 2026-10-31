"""Tests for MlflowSourceReader (Task 4 - issue #460)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.results.mlflow_source import MlflowSourceReader
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import (
    Cadence,
    CheckSpec,
    FileResults,
    HandoffPayload,
    MlflowResults,
    Operations,
)
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import projects
from mlops_agents.tracking.contract import (
    MetricPoint,
    ModelVersionRow,
    RunRow,
    SourceType,
    SyncResult,
)
from mlops_agents.tracking.service import TrackingService
from tests.conftest import MemoryKeyStore
from tests.monitoring.results.conftest import FakeConnections, FakeHandoffs, FakeObserver
from tests.proposals.samples import handoff as sample_handoff_dict
from tests.tracking.conftest import (
    FakeHandoffs as TrackingFakeHandoffs,
)
from tests.tracking.conftest import (
    FakeTrackingAdapter,
    TrackingEnv,
)
from tests.tracking.conftest import TestClock as ClockForTest


def _build_handoff_with_mlflow_checks(checks: list[CheckSpec] | None = None) -> HandoffView:
    ops = Operations(
        scheduler=None,
        feeds=[],
        on_call=[],
        checks=checks or [],
        results_location=FileResults(connection="conn_a", path_pattern="*.jsonl"),
    )
    payload = HandoffPayload.model_validate(sample_handoff_dict(operations=ops.model_dump()))
    return HandoffView(
        proposal_id="prop_mlflow",
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
    project_id = "prj_mlflowtest"
    engine = await databases.app()
    async with engine.begin() as conn:
        await conn.execute(
            insert(projects).values(
                id=project_id,
                name="MLflow Test Project",
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
    secrets: Any = None
    events = SqliteEventLog(databases)
    adapter = FakeTrackingAdapter("mlflow")
    adapters: dict[SourceType, FakeTrackingAdapter] = {"mlflow": adapter}
    handoffs = TrackingFakeHandoffs()

    async def _async_token() -> str | None:
        return "fake-token"

    service = TrackingService(
        databases=databases,
        keystore=keystore,
        secrets=secrets,
        events=events,
        adapters=adapters,
        github_token=_async_token,
        clock=clock,
        handoffs=handoffs,
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
        handoffs=handoffs,
    )


async def test_mlflow_metric_points_fire_above_and_below_threshold(
    tracking_env: TrackingEnv,
) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )

    t1 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 30, 10, 5, 0, tzinfo=UTC)
    t3 = datetime(2026, 10, 30, 10, 10, 0, tzinfo=UTC)

    runs = [
        RunRow(
            source_id=source.id,
            key="run_1",
            experiment="exp_1",
            status="succeeded",
            start=t1,
            end=t1,
            params={},
            tags={},
            git_commit=None,
            datasets=[],
        ),
        RunRow(
            source_id=source.id,
            key="run_2",
            experiment="exp_1",
            status="succeeded",
            start=t2,
            end=t2,
            params={},
            tags={},
            git_commit=None,
            datasets=[],
        ),
    ]

    metrics = [
        # Accuracy: threshold 0.85, fires_when="below"
        # point 1: 0.80 < 0.85 -> fires!
        MetricPoint(
            source_id=source.id, run_key="run_1", metric="accuracy", step=1, value=0.80, time=t1
        ),
        # point 2: 0.90 >= 0.85 -> does not fire
        MetricPoint(
            source_id=source.id, run_key="run_2", metric="accuracy", step=1, value=0.90, time=t2
        ),
        # Drift: threshold 0.05, fires_when="above"
        # point 3: 0.08 > 0.05 -> fires!
        MetricPoint(
            source_id=source.id, run_key="run_1", metric="drift_ks", step=1, value=0.08, time=t1
        ),
        # point 4: 0.02 <= 0.05 -> does not fire
        MetricPoint(
            source_id=source.id, run_key="run_2", metric="drift_ks", step=1, value=0.02, time=t2
        ),
        # Unbounded metric without threshold -> never fires
        MetricPoint(
            source_id=source.id, run_key="run_1", metric="loss", step=1, value=1.5, time=t3
        ),
    ]

    versions = [
        ModelVersionRow(
            source_id=source.id,
            model="churn_model",
            version="1.0.0",
            run_key="run_1",
            aliases=[],
            created=t1,
            evaluation="absent",
        )
    ]

    env.adapters["mlflow"].result = SyncResult(
        versions=versions,
        runs=runs,
        metrics=metrics,
        aliases={},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    checks = [
        CheckSpec(
            check_id="chk_acc",
            name="Accuracy Check",
            category="model_performance",
            cadence=Cadence(every_minutes=60, cron=None),
            timezone="UTC",
            results=MlflowResults(metric="accuracy", threshold=0.85, fires_when="below"),
        ),
        CheckSpec(
            check_id="chk_drift",
            name="Drift Check",
            category="drift",
            cadence=Cadence(every_minutes=60, cron=None),
            timezone="UTC",
            results=MlflowResults(metric="drift_ks", threshold=0.05, fires_when="above"),
        ),
    ]
    handoffs = FakeHandoffs(_build_handoff_with_mlflow_checks(checks))
    reader = MlflowSourceReader(tracking=env.service, handoffs=handoffs)

    # 1. Test accuracy source (fires_when below)
    acc_outcome = await reader.read(env.project_id, "mlflow:accuracy", cursor={})
    assert len(acc_outcome.records) == 2
    r_acc_1 = acc_outcome.records[0][1]
    assert r_acc_1.check_id == "chk_acc"
    assert r_acc_1.fired is True
    assert r_acc_1.value == 0.80
    assert r_acc_1.threshold == 0.85
    assert r_acc_1.model == "churn_model"  # Resolved from version
    assert r_acc_1.category == "model_performance"

    r_acc_2 = acc_outcome.records[1][1]
    assert r_acc_2.check_id == "chk_acc"
    assert r_acc_2.fired is False
    assert r_acc_2.value == 0.90
    assert r_acc_2.model is None  # run_2 not in versions

    # 2. Test drift source (fires_when above)
    drift_outcome = await reader.read(env.project_id, "mlflow:drift_ks", cursor={})
    assert len(drift_outcome.records) == 2
    r_drift_1 = drift_outcome.records[0][1]
    assert r_drift_1.check_id == "chk_drift"
    assert r_drift_1.fired is True
    assert r_drift_1.value == 0.08
    assert r_drift_1.category == "drift"

    r_drift_2 = drift_outcome.records[1][1]
    assert r_drift_2.fired is False
    assert r_drift_2.value == 0.02

    # 3. Test metric without threshold
    loss_outcome = await reader.read(env.project_id, "mlflow:loss", cursor={})
    assert len(loss_outcome.records) == 1
    r_loss = loss_outcome.records[0][1]
    assert r_loss.fired is False
    assert r_loss.threshold is None
    assert r_loss.check_id == "mlflow:loss"


async def test_mlflow_running_run_holds_cursor_and_reread_stores_nothing_twice(
    tracking_env: TrackingEnv,
) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )

    t1 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 30, 10, 10, 0, tzinfo=UTC)

    run_running = RunRow(
        source_id=source.id,
        key="run_act",
        experiment="exp_1",
        status="running",
        start=t1,
        end=None,
        params={},
        tags={},
        git_commit=None,
        datasets=[],
    )
    metric_p1 = MetricPoint(
        source_id=source.id, run_key="run_act", metric="eval_loss", step=1, value=0.45, time=t1
    )

    env.adapters["mlflow"].result = SyncResult(
        versions=[],
        runs=[run_running],
        metrics=[metric_p1],
        aliases={},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    checks = [
        CheckSpec(
            check_id="chk_eval_loss",
            name="Loss Check",
            category="model_performance",
            cadence=Cadence(every_minutes=60, cron=None),
            timezone="UTC",
            results=MlflowResults(metric="eval_loss", threshold=0.5, fires_when="above"),
        )
    ]
    handoffs = FakeHandoffs(_build_handoff_with_mlflow_checks(checks))
    reader = MlflowSourceReader(tracking=env.service, handoffs=handoffs)

    # Initial read while run is running
    outcome_1 = await reader.read(env.project_id, "mlflow:eval_loss", cursor={})
    assert len(outcome_1.records) == 1
    # Cursor is held at running run start
    assert outcome_1.cursor.get("since") == t1.isoformat()

    # Collect with ResultsService
    service = ResultsService(
        databases=env.databases,
        handoffs=handoffs,
        connections=FakeConnections(),
        events=env.events,
        tracking=env.service,
        mlflow_reader=reader,
    )
    obs = FakeObserver()
    service.add_observer(obs)

    clock_dt = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
    rep_1 = await service.collect(env.project_id, clock_dt)
    assert rep_1.stored == 1
    assert len(obs.batches) == 1

    # Second collect while run is still running: re-reading stores nothing twice!
    rep_2 = await service.collect(env.project_id, clock_dt)
    assert rep_2.stored == 0
    assert len(obs.batches) == 1  # observer not called again

    # Run finishes, second run arrives
    run_finished = RunRow(
        source_id=source.id,
        key="run_act",
        experiment="exp_1",
        status="succeeded",
        start=t1,
        end=t2,
        params={},
        tags={},
        git_commit=None,
        datasets=[],
    )
    env.adapters["mlflow"].result = SyncResult(
        versions=[],
        runs=[run_finished],
        metrics=[metric_p1],
        aliases={},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    outcome_fin = await reader.read(env.project_id, "mlflow:eval_loss", cursor=outcome_1.cursor)
    # Cursor can now advance since all runs are finished
    assert outcome_fin.cursor.get("since") == t1.isoformat()


async def test_mlflow_metrics_query_error_stops_before_run_and_retains_cursor(
    tracking_env: TrackingEnv,
    tmp_path: Path,
) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )

    t1 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 30, 10, 10, 0, tzinfo=UTC)

    run_1 = RunRow(
        source_id=source.id,
        key="run_1",
        experiment="exp_1",
        status="succeeded",
        start=t1,
        end=t1,
        params={},
        tags={},
        git_commit=None,
        datasets=[],
    )
    run_2 = RunRow(
        source_id=source.id,
        key="run_2",
        experiment="exp_1",
        status="succeeded",
        start=t2,
        end=t2,
        params={},
        tags={},
        git_commit=None,
        datasets=[],
    )

    metric_p1 = MetricPoint(
        source_id=source.id, run_key="run_1", metric="loss", step=1, value=0.5, time=t1
    )
    metric_p2 = MetricPoint(
        source_id=source.id, run_key="run_2", metric="loss", step=1, value=0.3, time=t2
    )

    env.adapters["mlflow"].result = SyncResult(
        versions=[],
        runs=[run_1, run_2],
        metrics=[metric_p1, metric_p2],
        aliases={},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    check = CheckSpec(
        check_id="chk_loss",
        name="Loss Check",
        category="model_performance",
        cadence=Cadence(cron=None, every_minutes=60),
        timezone="UTC",
        results=MlflowResults(metric="loss", threshold=None, fires_when=None),
    )
    handoffs = FakeHandoffs(_build_handoff_with_mlflow_checks([check]))
    reader = MlflowSourceReader(tracking=env.service, handoffs=handoffs)

    conn_dir = tmp_path / "dummy_conn_a"
    conn_dir.mkdir(parents=True, exist_ok=True)
    connections = FakeConnections()
    connections.add_local_folder("conn_a", conn_dir)

    original_query = env.service.query
    should_fail_run_2 = True

    async def flaky_query(*args: Any, **kwargs: Any) -> Any:
        query = args[1] if len(args) > 1 else kwargs.get("query")
        if (
            getattr(query, "what", None) == "metrics"
            and getattr(query, "run_key", None) == "run_2"
            and should_fail_run_2
        ):
            raise RuntimeError("Transient failure fetching metrics for run_2")
        return await original_query(*args, **kwargs)

    env.service.query = flaky_query  # type: ignore[method-assign]

    initial_cursor = {"since": datetime(2026, 10, 30, 9, 0, 0, tzinfo=UTC).isoformat()}

    # Direct reader check:
    outcome_direct = await reader.read(env.project_id, "mlflow:loss", cursor=initial_cursor)
    assert len(outcome_direct.records) == 1
    assert outcome_direct.cursor == initial_cursor
    assert outcome_direct.consecutive_failures == 1
    assert outcome_direct.last_error is not None
    assert "Transient failure" in outcome_direct.last_error

    # End-to-end ResultsService check:
    results_service = ResultsService(
        databases=env.databases,
        handoffs=handoffs,
        connections=connections,
        events=env.events,
        tracking=env.service,
        mlflow_reader=reader,
    )

    # Tick 1: run_1 succeeds, run_2 fails during metrics query
    rep_1 = await results_service.collect(env.project_id, t1)
    assert rep_1.stored == 1
    assert "mlflow:loss" in rep_1.failures
    st1 = await results_service.store.get_source_state(env.project_id, "mlflow:loss")
    assert st1 is not None
    assert st1.cursor == {}  # ResultsService started with empty cursor in DB
    assert st1.consecutive_failures == 1

    # Tick 2: Transient error resolved, re-reading with the retained cursor
    should_fail_run_2 = False
    rep_2 = await results_service.collect(env.project_id, t2)
    assert rep_2.stored == 1  # Only run_2 is newly stored (run_1 deduplicated)
    assert "mlflow:loss" not in rep_2.failures
    st2 = await results_service.store.get_source_state(env.project_id, "mlflow:loss")
    assert st2 is not None
    assert st2.cursor.get("since") == t2.isoformat()
    assert st2.consecutive_failures == 0
    assert st2.last_error is None


async def test_mlflow_source_reader_scrubs_credentials_in_all_failure_sites(
    tracking_env: TrackingEnv,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )

    t1 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
    run_1 = RunRow(
        source_id=source.id,
        key="run_1",
        experiment="exp_1",
        status="succeeded",
        start=t1,
        end=t1,
        params={},
        tags={},
        git_commit=None,
        datasets=[],
    )
    metric_p1 = MetricPoint(
        source_id=source.id, run_key="run_1", metric="loss", step=1, value=0.5, time=t1
    )
    env.adapters["mlflow"].result = SyncResult(
        versions=[],
        runs=[run_1],
        metrics=[metric_p1],
        aliases={},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    # 1. Site 1: Source id parse failure carrying credential
    secret_s1 = "site1_secret_pass_111"
    raw_s1_url = f"https://user:{secret_s1}@mlflow.discovery/api?token=tok_s1"
    handoffs = FakeHandoffs(_build_handoff_with_mlflow_checks([]))
    reader = MlflowSourceReader(tracking=env.service, handoffs=handoffs)

    out_s1 = await reader.read(env.project_id, f"invalid_source_{raw_s1_url}", cursor={})
    assert out_s1.last_error is not None
    assert secret_s1 not in out_s1.last_error
    assert "tok_s1" not in out_s1.last_error
    assert "***@" in out_s1.last_error

    # 2. Site 2: Runs query failure carrying credential
    secret_s2 = "site2_secret_token_222"
    raw_s2_error = f"Runs query failed at https://user:{secret_s2}@mlflow.runs/api?token=tok_s2"

    original_query = env.service.query

    async def runs_failing_query(*args: Any, **kwargs: Any) -> Any:
        query = args[1] if len(args) > 1 else kwargs.get("query")
        if getattr(query, "what", None) == "runs":
            raise RuntimeError(raw_s2_error)
        return await original_query(*args, **kwargs)

    env.service.query = runs_failing_query  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        out_s2 = await reader.read(env.project_id, "mlflow:loss", cursor={})
    assert out_s2.last_error is not None
    assert secret_s2 not in out_s2.last_error
    assert "tok_s2" not in out_s2.last_error
    assert "***@" in out_s2.last_error
    assert secret_s2 not in caplog.text
    assert "tok_s2" not in caplog.text

    # 3. Site 3: Metrics query failure carrying credential
    secret_s3 = "site3_secret_token_333"
    raw_s3_error = (
        f"Metrics query failed at https://user:{secret_s3}@mlflow.metrics/api?token=tok_s3"
    )

    async def metrics_failing_query(*args: Any, **kwargs: Any) -> Any:
        query = args[1] if len(args) > 1 else kwargs.get("query")
        if getattr(query, "what", None) == "metrics":
            raise RuntimeError(raw_s3_error)
        return await original_query(*args, **kwargs)

    env.service.query = metrics_failing_query  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        out_s3 = await reader.read(env.project_id, "mlflow:loss", cursor={})
    assert out_s3.last_error is not None
    assert secret_s3 not in out_s3.last_error
    assert "tok_s3" not in out_s3.last_error
    assert "***@" in out_s3.last_error
    assert secret_s3 not in caplog.text
    assert "tok_s3" not in caplog.text
