"""Tests for tracking queries: pagination, filtering, latest metrics, typed results."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from mlops_agents.errors import DomainError
from mlops_agents.tracking.contract import (
    CiRunRow,
    DeploymentRow,
    EvaluationRecord,
    MetricPoint,
    ModelSummary,
    ModelVersionRow,
    RunRow,
    SyncResult,
    TrackingQuery,
)
from tests.tracking.conftest import TrackingEnv


async def test_query_unknown_project(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    with pytest.raises(DomainError) as exc_info:
        await env.service.query("prj_unknown", TrackingQuery(what="models"))
    assert exc_info.value.status == 404
    assert exc_info.value.code == "project_not_found"


def test_query_limit_validation() -> None:
    # Bounds live on TrackingQuery contract: > 500, < 1, or offset < 0 raise ValidationError
    with pytest.raises(ValidationError):
        TrackingQuery(what="models", limit=501)
    with pytest.raises(ValidationError):
        TrackingQuery(what="models", limit=0)
    with pytest.raises(ValidationError):
        TrackingQuery(what="models", offset=-1)


async def test_query_metrics_requires_run_key(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    with pytest.raises(DomainError) as exc_info:
        await env.service.query(env.project_id, TrackingQuery(what="metrics", run_key=None))
    assert exc_info.value.status == 422
    assert exc_info.value.code == "tracking_bad_query"


async def test_query_models_and_summary_details(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )
    t0 = datetime(2026, 10, 28, 10, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 10, 28, 11, 0, 0, tzinfo=UTC)

    # Model A: 2 versions, version 1 has production alias
    # Model B: 1 version, no alias
    env.adapters["mlflow"].result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=source.id,
                model="model_a",
                version="1",
                run_key=None,
                aliases=[],
                created=t0,
                evaluation="absent",
            ),
            ModelVersionRow(
                source_id=source.id,
                model="model_a",
                version="2",
                run_key=None,
                aliases=[],
                created=t1,
                evaluation="absent",
            ),
            ModelVersionRow(
                source_id=source.id,
                model="model_b",
                version="1",
                run_key=None,
                aliases=[],
                created=t0,
                evaluation="absent",
            ),
        ],
        runs=[],
        metrics=[],
        aliases={"model_a": {"production": "1"}},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    page = await env.service.query(env.project_id, TrackingQuery(what="models"))
    assert page.total == 2
    assert page.truncated is False
    assert len(page.rows) == 2
    for r in page.rows:
        assert isinstance(r, ModelSummary)

    models_by_name = {m.name: m for m in page.rows}
    assert models_by_name["model_a"].production_version == "1"
    assert models_by_name["model_a"].latest_version == "2"
    assert models_by_name["model_a"].versions == 2

    assert models_by_name["model_b"].production_version is None
    assert models_by_name["model_b"].latest_version == "1"
    assert models_by_name["model_b"].versions == 1


async def test_query_pagination_over_120_rows(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )
    t0 = datetime(2026, 10, 28, 10, 0, 0, tzinfo=UTC)

    # Insert 120 runs
    runs = [
        RunRow(
            source_id=source.id,
            key=f"run_{i:03d}",
            experiment="exp",
            status="succeeded",
            start=t0 + timedelta(seconds=i),
            end=None,
            params={},
            tags={},
            git_commit=None,
            datasets=[],
        )
        for i in range(120)
    ]
    env.adapters["mlflow"].result = SyncResult(
        versions=[], runs=runs, metrics=[], aliases={}, ci_runs=[], evaluations=[], error=None
    )
    await env.service.run_sync(env.project_id, source.id)

    # Page 1 (offset=0, limit=50)
    p1 = await env.service.query(env.project_id, TrackingQuery(what="runs", offset=0, limit=50))
    assert p1.total == 120
    assert p1.truncated is True
    assert len(p1.rows) == 50

    # Page 2 (offset=50, limit=50)
    p2 = await env.service.query(env.project_id, TrackingQuery(what="runs", offset=50, limit=50))
    assert p2.total == 120
    assert p2.truncated is True
    assert len(p2.rows) == 50

    # Page 3 (offset=100, limit=50)
    p3 = await env.service.query(env.project_id, TrackingQuery(what="runs", offset=100, limit=50))
    assert p3.total == 120
    assert p3.truncated is False
    assert len(p3.rows) == 20

    # Disjoint check
    p1_keys = {r.key for r in p1.rows}
    p2_keys = {r.key for r in p2.rows}
    p3_keys = {r.key for r in p3.rows}

    assert len(p1_keys & p2_keys) == 0
    assert len(p2_keys & p3_keys) == 0
    assert len(p1_keys | p2_keys | p3_keys) == 120


async def test_query_metrics_latest(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )
    t0 = datetime(2026, 10, 28, 10, 0, 0, tzinfo=UTC)

    # Metric m1: step 1 (0.5), step 2 (0.8)
    # Metric m2: step 1 (100.0)
    metrics = [
        MetricPoint(source_id=source.id, run_key="r1", metric="m1", step=1, value=0.5, time=t0),
        MetricPoint(
            source_id=source.id,
            run_key="r1",
            metric="m1",
            step=2,
            value=0.8,
            time=t0 + timedelta(seconds=10),
        ),
        MetricPoint(source_id=source.id, run_key="r1", metric="m2", step=1, value=100.0, time=t0),
    ]
    env.adapters["mlflow"].result = SyncResult(
        versions=[], runs=[], metrics=metrics, aliases={}, ci_runs=[], evaluations=[], error=None
    )
    await env.service.run_sync(env.project_id, source.id)

    # Query all points
    p_all = await env.service.query(
        env.project_id, TrackingQuery(what="metrics", run_key="r1", latest=False)
    )
    assert p_all.total == 3
    assert len(p_all.rows) == 3

    # Query latest points
    p_latest = await env.service.query(
        env.project_id, TrackingQuery(what="metrics", run_key="r1", latest=True)
    )
    assert p_latest.total == 2
    assert len(p_latest.rows) == 2
    by_metric = {m.metric: m for m in p_latest.rows}
    assert by_metric["m1"].step == 2
    assert by_metric["m1"].value == 0.8
    assert by_metric["m2"].step == 1


async def test_query_each_what_typed(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )
    t0 = datetime(2026, 10, 28, 10, 0, 0, tzinfo=UTC)

    env.adapters["mlflow"].result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=source.id,
                model="mod",
                version="1",
                run_key="r1",
                aliases=[],
                created=t0,
                evaluation="present",
            )
        ],
        runs=[
            RunRow(
                source_id=source.id,
                key="r1",
                experiment="exp",
                status="succeeded",
                start=t0,
                end=None,
                params={},
                tags={},
                git_commit=None,
                datasets=[],
            )
        ],
        metrics=[
            MetricPoint(
                source_id=source.id, run_key="r1", metric="loss", step=1, value=0.1, time=t0
            )
        ],
        aliases={"mod": {"production": "1"}},
        ci_runs=[
            CiRunRow(
                source_id=source.id,
                key="c1",
                workflow="ci.yml",
                status="succeeded",
                start=t0,
                duration_s=12.0,
                commit="c123",
                url="http://ci",
            )
        ],
        evaluations=[
            EvaluationRecord(
                source_id=source.id,
                model="mod",
                version="1",
                run_key="r1",
                schema_version=1,
                payload={"loss": 0.1},
                fetched_at=t0,
            )
        ],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    # Check each what
    p_ver = await env.service.query(env.project_id, TrackingQuery(what="versions", model="mod"))
    assert isinstance(p_ver.rows[0], ModelVersionRow)

    p_run = await env.service.query(env.project_id, TrackingQuery(what="runs", run_key="r1"))
    assert isinstance(p_run.rows[0], RunRow)

    p_dep = await env.service.query(env.project_id, TrackingQuery(what="deployments", model="mod"))
    assert isinstance(p_dep.rows[0], DeploymentRow)

    p_ci = await env.service.query(env.project_id, TrackingQuery(what="ci_runs"))
    assert isinstance(p_ci.rows[0], CiRunRow)

    p_eval = await env.service.query(env.project_id, TrackingQuery(what="evaluations", model="mod"))
    assert isinstance(p_eval.rows[0], EvaluationRecord)


async def test_query_since_filter_with_timezone_normalization(
    tracking_env: TrackingEnv,
) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "http://testserver"},
        auth="none",
    )
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 10, 28, 14, 0, 0, tzinfo=UTC)

    adapter = env.adapters["mlflow"]
    adapter.result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=source.id,
                model="M",
                version="1",
                run_key=None,
                aliases=[],
                created=t0,
                evaluation="absent",
            ),
            ModelVersionRow(
                source_id=source.id,
                model="M",
                version="2",
                run_key=None,
                aliases=[],
                created=t1,
                evaluation="absent",
            ),
        ],
        runs=[
            RunRow(
                source_id=source.id,
                key="r1",
                experiment="exp",
                status="succeeded",
                start=t0,
                end=t0,
                params={},
                tags={},
                git_commit=None,
                datasets=[],
            ),
            RunRow(
                source_id=source.id,
                key="r2",
                experiment="exp",
                status="succeeded",
                start=t1,
                end=t1,
                params={},
                tags={},
                git_commit=None,
                datasets=[],
            ),
        ],
        metrics=[],
        aliases={},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    # Filter with since as 15:00 in +02:00 (= 13:00 UTC).
    # t0 (12:00 UTC) should be excluded, t1 (14:00 UTC) should be included.
    tz_plus_2 = timezone(timedelta(hours=2))
    since_dt = datetime(2026, 10, 28, 15, 0, 0, tzinfo=tz_plus_2)

    page = await env.service.query(
        env.project_id, TrackingQuery(what="versions", model="M", since=since_dt)
    )
    assert len(page.rows) == 1
    assert page.rows[0].version == "2"

    page_runs = await env.service.query(env.project_id, TrackingQuery(what="runs", since=since_dt))
    assert len(page_runs.rows) == 1
    assert page_runs.rows[0].key == "r2"

    # Naive datetime is treated as UTC (13:00 UTC) rather than local system timezone
    naive_since = datetime(2026, 10, 28, 13, 0, 0)
    page_naive = await env.service.query(
        env.project_id, TrackingQuery(what="versions", model="M", since=naive_since)
    )
    assert len(page_naive.rows) == 1
    assert page_naive.rows[0].version == "2"


async def test_query_source_id_filtering(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    s1 = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "http://s1"},
        auth="none",
    )
    s2 = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "http://s2"},
        auth="none",
    )
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
    adapter = env.adapters["mlflow"]
    adapter.result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=s1.id,
                model="Model1",
                version="1",
                run_key=None,
                aliases=[],
                created=t0,
                evaluation="absent",
            ),
        ],
        runs=[
            RunRow(
                source_id=s1.id,
                key="run1",
                experiment="exp",
                status="succeeded",
                start=t0,
                end=t0,
                params={},
                tags={},
                git_commit=None,
                datasets=[],
            ),
        ],
        metrics=[],
        aliases={},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, s1.id)

    adapter.result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=s2.id,
                model="Model2",
                version="1",
                run_key=None,
                aliases=[],
                created=t0,
                evaluation="absent",
            ),
        ],
        runs=[
            RunRow(
                source_id=s2.id,
                key="run2",
                experiment="exp",
                status="succeeded",
                start=t0,
                end=t0,
                params={},
                tags={},
                git_commit=None,
                datasets=[],
            ),
        ],
        metrics=[],
        aliases={},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, s2.id)

    # Query without source_id filter -> returns both
    all_models = await env.service.query(env.project_id, TrackingQuery(what="models"))
    assert len(all_models.rows) == 2

    # Query with source_id filter -> returns only matching source
    s1_models = await env.service.query(
        env.project_id, TrackingQuery(what="models", source_id=s1.id)
    )
    assert len(s1_models.rows) == 1
    assert s1_models.rows[0].source_id == s1.id

    s2_models = await env.service.query(
        env.project_id, TrackingQuery(what="models", source_id=s2.id)
    )
    assert len(s2_models.rows) == 1
    assert s2_models.rows[0].source_id == s2.id

    s1_runs = await env.service.query(env.project_id, TrackingQuery(what="runs", source_id=s1.id))
    assert len(s1_runs.rows) == 1
    assert s1_runs.rows[0].source_id == s1.id
