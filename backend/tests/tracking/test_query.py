"""Tests for tracking queries: pagination, filtering, latest metrics, typed results."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mlops_agents.errors import DomainError
from mlops_agents.proposals.payloads import HandoffPayload
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
from tests.proposals.samples import handoff, model
from tests.tracking.conftest import TrackingEnv


async def test_query_unknown_project(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    with pytest.raises(DomainError) as exc_info:
        await env.service.query("prj_unknown", TrackingQuery(what="models"))
    assert exc_info.value.status == 404
    assert exc_info.value.code == "project_not_found"


async def test_query_limit_validation(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    # Limit > 500
    with pytest.raises(DomainError) as exc_info:
        await env.service.query(
            env.project_id, TrackingQuery.model_construct(what="models", limit=501)
        )
    assert exc_info.value.status == 422
    assert exc_info.value.code == "tracking_bad_query"


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


async def test_query_production_alias_rules_with_handoffs(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )
    t0 = datetime(2026, 10, 28, 10, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 10, 28, 11, 0, 0, tzinfo=UTC)

    # Sync model_x with v1 (alias 'production') and v2 (alias 'live')
    # model_y with v1 (alias 'staging') - no production alias
    env.adapters["mlflow"].result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=source.id,
                model="model_x",
                version="1",
                run_key=None,
                aliases=[],
                created=t0,
                evaluation="absent",
            ),
            ModelVersionRow(
                source_id=source.id,
                model="model_x",
                version="2",
                run_key=None,
                aliases=[],
                created=t1,
                evaluation="absent",
            ),
            ModelVersionRow(
                source_id=source.id,
                model="model_y",
                version="1",
                run_key=None,
                aliases=[],
                created=t0,
                evaluation="absent",
            ),
        ],
        runs=[],
        metrics=[],
        aliases={
            "model_x": {"production": "1", "live": "2"},
            "model_y": {"staging": "1"},
        },
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    # 1. No handoff yet: fallback to PRODUCTION_ALIAS ("production")
    # model_x holding 'production' -> v1
    # model_y no 'production' -> None
    page = await env.service.query(env.project_id, TrackingQuery(what="models"))
    by_name = {m.name: m for m in page.rows}
    assert by_name["model_x"].production_version == "1"
    assert by_name["model_y"].production_version is None

    # 2. Handoff mapping alias 'live' to 'production' environment for model_x
    # model_y has no spec in this handoff -> fallback to PRODUCTION_ALIAS -> None
    p1 = HandoffPayload.model_validate(
        handoff(
            models=[
                model(
                    "model_x",
                    environments={"live": "production", "preview": "staging"},
                    production_signal=None,
                )
            ]
        )
    )
    await env.handoffs.store(env.project_id, "prp_1", p1)

    # Without a sync, querying models immediately uses the new handoff!
    page = await env.service.query(env.project_id, TrackingQuery(what="models"))
    by_name = {m.name: m for m in page.rows}
    assert by_name["model_x"].production_version == "2"
    assert by_name["model_y"].production_version is None

    # 3. New handoff mapping both 'canary' and 'live' to 'production'
    # Candidate aliases sorted: ['canary', 'live'].
    # If no version has 'canary', first existing is 'live' -> v2.
    # If a new handoff maps only 'champion' to 'production', but no version holds 'champion' -> None
    p2 = HandoffPayload.model_validate(
        handoff(
            models=[
                model(
                    "model_x",
                    environments={"champion": "production"},
                    production_signal=None,
                )
            ]
        )
    )
    await env.handoffs.store(env.project_id, "prp_2", p2)
    page = await env.service.query(env.project_id, TrackingQuery(what="models"))
    by_name = {m.name: m for m in page.rows}
    assert by_name["model_x"].production_version is None

    # 4. Handoff raises an exception: falls back to PRODUCTION_ALIAS ("production")
    env.handoffs.error = RuntimeError("storage unavailable")
    page = await env.service.query(env.project_id, TrackingQuery(what="models"))
    by_name = {m.name: m for m in page.rows}
    assert by_name["model_x"].production_version == "1"
