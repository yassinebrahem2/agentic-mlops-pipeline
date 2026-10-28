"""Tests for the MLflow tracking adapter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from fastapi import FastAPI, Response
from fastapi.responses import PlainTextResponse

from mlops_agents.tracking.adapters.mlflow import (
    EVALUATION_MAX_BYTES,
    MlflowAdapter,
)
from mlops_agents.tracking.contract import (
    TrackingQuery,
    TrackingSourceView,
)
from mlops_agents.tracking.service import TrackingService
from tests.fakes.fake_mlflow import FakeMlflowState, create_fake_mlflow_app
from tests.tracking.conftest import TrackingEnv


def _make_source_view(
    source_id: str = "src_mlflow_1",
    url: str = "http://testserver",
    auth: str = "none",
    credential_set: bool = False,
    last_sync: datetime | None = None,
) -> TrackingSourceView:
    return TrackingSourceView(
        id=source_id,
        type="mlflow",
        settings={"url": url},
        auth=auth,  # type: ignore[arg-type]
        credential_set=credential_set,
        last_sync=last_sync,
        last_error=None,
    )


@pytest.mark.asyncio
async def test_adapter_mappings_and_filtering() -> None:
    state = FakeMlflowState()
    app = create_fake_mlflow_app(state)

    exp_id = state.add_experiment("Default")

    # Finished run
    t0_ms = 1700000000000
    t1_ms = 1700000010000
    tags = {"mlflow.source.git.commit": "commit_123456"}
    for i in range(120):
        tags[f"tag_{i}"] = f"v_{i}" * 100  # long values
    params = {"learning_rate": "0.01", "long_p": "a" * 600}
    state.add_run(
        experiment_id=exp_id,
        run_id="run_finished",
        status="FINISHED",
        start_time=t0_ms,
        end_time=t1_ms,
        params=params,
        tags=tags,
        datasets=["training_set", "eval_set"],
    )

    # Running run
    t2_ms = 1700000020000
    state.add_run(
        experiment_id=exp_id,
        run_id="run_running",
        status="RUNNING",
        start_time=t2_ms,
        end_time=None,
    )
    state.log_metric("run_running", "val_loss", 0.42, timestamp=t2_ms + 1000, step=1)
    state.log_metric("run_running", "val_loss", 0.38, timestamp=t2_ms + 2000, step=2)

    # Other statuses
    state.add_run(experiment_id=exp_id, run_id="run_sched", status="SCHEDULED")
    state.add_run(experiment_id=exp_id, run_id="run_failed", status="FAILED")
    state.add_run(experiment_id=exp_id, run_id="run_killed", status="KILLED")
    state.add_run(experiment_id=exp_id, run_id="run_other", status="SOME_CUSTOM_STATUS")

    # Registered models
    state.register_version("model_x", "1")
    state.register_version("model_x", "2")
    state.set_alias("model_x", "champion", "2")

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        adapter = MlflowAdapter(http=client)
        source = _make_source_view()
        result = await adapter.sync(source, credential=None, since=None)

    assert result.error is None
    assert len(result.runs) == 6
    run_map = {r.key: r for r in result.runs}

    # Status normalization
    assert run_map["run_finished"].status == "succeeded"
    assert run_map["run_running"].status == "running"
    assert run_map["run_sched"].status == "running"
    assert run_map["run_failed"].status == "failed"
    assert run_map["run_killed"].status == "cancelled"
    assert run_map["run_other"].status == "other"

    # Datetime conversion
    assert run_map["run_finished"].start == datetime.fromtimestamp(t0_ms / 1000.0, tz=UTC)
    assert run_map["run_finished"].end == datetime.fromtimestamp(t1_ms / 1000.0, tz=UTC)
    assert run_map["run_running"].end is None

    # Git commit & datasets
    assert run_map["run_finished"].git_commit == "commit_123456"
    assert run_map["run_finished"].datasets == ["training_set", "eval_set"]

    # Param and tag truncation
    assert len(run_map["run_finished"].params["long_p"]) == 500
    assert len(run_map["run_finished"].tags) == 100
    for v in run_map["run_finished"].tags.values():
        assert len(v) <= 500

    # Aliases
    assert result.aliases == {"model_x": {"champion": "2"}}
    version_map = {v.version: v for v in result.versions if v.model == "model_x"}
    assert version_map["2"].aliases == ["champion"]
    assert version_map["1"].aliases == []

    # Metrics
    assert len(result.metrics) == 2
    steps = [m.step for m in result.metrics]
    assert steps == [1, 2]


@pytest.mark.asyncio
async def test_adapter_paging_all_searches() -> None:
    state = FakeMlflowState(page_size=2)
    app = create_fake_mlflow_app(state)

    for i in range(5):
        exp_id = state.add_experiment(f"exp_{i}")
        run_id = state.add_run(experiment_id=exp_id, run_id=f"run_{i}", start_time=1000 + i * 10)
        state.log_metric(run_id, "loss", 0.5, step=1)
        state.register_version(f"model_{i}", "1")

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        adapter = MlflowAdapter(http=client)
        source = _make_source_view()
        result = await adapter.sync(source, credential=None, since=None)

    assert result.error is None
    assert len(result.runs) == 5
    assert len(result.versions) == 5
    assert len(result.metrics) == 5


@pytest.mark.asyncio
async def test_adapter_since_filtering() -> None:
    state = FakeMlflowState()
    app = create_fake_mlflow_app(state)

    exp_id = state.add_experiment("Default")
    t_end = 1700000000000
    run_fin = state.add_run(
        experiment_id=exp_id,
        run_id="run_old_fin",
        status="FINISHED",
        start_time=t_end - 5000,
        end_time=t_end,
    )
    state.log_metric(run_fin, "metric_a", 1.0, step=1)

    run_run = state.add_run(
        experiment_id=exp_id,
        run_id="run_active",
        status="RUNNING",
        start_time=t_end + 1000,
    )
    state.log_metric(run_run, "metric_b", 2.0, step=1)

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        adapter = MlflowAdapter(http=client)
        source = _make_source_view()

        # Since is set 3 days after run_old_fin ended
        since_dt = datetime.fromtimestamp(t_end / 1000.0, tz=UTC) + timedelta(days=3)
        state.requests.clear()
        result = await adapter.sync(source, credential=None, since=since_dt)

        assert result.error is None
        # Both runs returned in runs list
        assert {r.key for r in result.runs} == {"run_old_fin", "run_active"}
        # But metric history only fetched for the active running run!
        metric_reqs = [r for r in state.requests if r["path"].endswith("metrics/get-history")]
        assert len(metric_reqs) == 1
        assert "run_id=run_active" in metric_reqs[0]["query"]

        # If since is None: all histories read
        state.requests.clear()
        res_all = await adapter.sync(source, credential=None, since=None)
        assert res_all.error is None
        metric_reqs_all = [r for r in state.requests if r["path"].endswith("metrics/get-history")]
        assert len(metric_reqs_all) == 2


@pytest.mark.asyncio
async def test_adapter_errors() -> None:
    state = FakeMlflowState(required_token="good_secret")
    app = create_fake_mlflow_app(state)

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        adapter = MlflowAdapter(http=client)

        # 401 with bad token
        source = _make_source_view()
        res_bad_auth = await adapter.sync(source, credential="wrong_secret", since=None)
        assert res_bad_auth.error == "The tracker rejected the credential"
        assert res_bad_auth.runs == []

        # 500 error
        state.required_token = None
        state.fail_next["experiments/search"] = 500
        res_500 = await adapter.sync(source, credential=None, since=None)
        assert res_500.error == "MLflow answered with HTTP 500"

    # Unreachable port
    transport_closed = httpx2.AsyncHTTPTransport()
    async with httpx2.AsyncClient(
        transport=transport_closed, base_url="http://127.0.0.1:1"
    ) as client:
        adapter_unreach = MlflowAdapter(http=client)
        source_unreach = _make_source_view(url="http://127.0.0.1:1")
        res_unreach = await adapter_unreach.sync(source_unreach, credential=None, since=None)
        assert res_unreach.error is not None
        assert "127.0.0.1" in res_unreach.error or "connect" in res_unreach.error.lower()

    # Non-JSON body
    bad_json_app = FastAPI()

    @bad_json_app.post("/api/2.0/mlflow/experiments/search")
    async def bad_exp_search() -> Response:
        return PlainTextResponse("not valid json", status_code=200)

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=bad_json_app), base_url="http://testserver"
    ) as client:
        adapter_bad = MlflowAdapter(http=client)
        res_bad_json = await adapter_bad.sync(source, credential=None, since=None)
        assert res_bad_json.error == "MLflow answered with data this app cannot read"

    # Redirect is not followed (302)
    redirect_app = FastAPI()

    @redirect_app.post("/api/2.0/mlflow/experiments/search")
    async def redirect_exp_search() -> Response:
        return Response(status_code=302, headers={"location": "http://127.0.0.1:9999/somewhere"})

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=redirect_app), base_url="http://testserver"
    ) as client:
        adapter_redir = MlflowAdapter(http=client)
        res_redir = await adapter_redir.sync(source, credential=None, since=None)
        assert res_redir.error == "MLflow answered with HTTP 302"


@pytest.mark.asyncio
async def test_adapter_evaluations() -> None:
    state = FakeMlflowState()
    app = create_fake_mlflow_app(state)

    state.add_experiment("Default")
    # v1: valid evaluation artifact
    state.add_run(experiment_id="0", run_id="r1")
    state.register_version("model_eval", "1", run_id="r1")
    state.put_artifact(
        "r1", "mlops_agents/evaluation.json", b'{"schema_version": 1, "accuracy": 0.95}'
    )

    # v2: 404 artifact
    state.add_run(experiment_id="0", run_id="r2")
    state.register_version("model_eval", "2", run_id="r2")

    # v3: version with no run_id
    state.register_version("model_eval", "3", run_id="")

    # v4: missing schema_version
    state.add_run(experiment_id="0", run_id="r4")
    state.register_version("model_eval", "4", run_id="r4")
    state.put_artifact("r4", "mlops_agents/evaluation.json", b'{"accuracy": 0.99}')

    # v5: non-object json
    state.add_run(experiment_id="0", run_id="r5")
    state.register_version("model_eval", "5", run_id="r5")
    state.put_artifact("r5", "mlops_agents/evaluation.json", b"[1, 2, 3]")

    # v6: oversized body
    state.add_run(experiment_id="0", run_id="r6")
    state.register_version("model_eval", "6", run_id="r6")
    oversized = b'{"schema_version": 1, "data": "' + b"x" * (EVALUATION_MAX_BYTES + 10) + b'"}'
    state.put_artifact("r6", "mlops_agents/evaluation.json", oversized)

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        adapter = MlflowAdapter(http=client)
        source = _make_source_view()
        result = await adapter.sync(source, credential=None, since=None)

    assert result.error is None
    version_map = {v.version: v for v in result.versions}
    assert version_map["1"].evaluation == "present"
    assert version_map["2"].evaluation == "absent"
    assert version_map["3"].evaluation == "absent"
    assert version_map["4"].evaluation == "fetch_failed"
    assert version_map["5"].evaluation == "fetch_failed"
    assert version_map["6"].evaluation == "fetch_failed"

    assert len(result.evaluations) == 1
    eval_rec = result.evaluations[0]
    assert eval_rec.run_key == "r1"
    assert eval_rec.schema_version == 1
    assert eval_rec.payload == {"schema_version": 1, "accuracy": 0.95}

    # 500 error on artifact fetch gives fetch_failed for only that version
    state.fail_next["/get-artifact"] = 500
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        adapter = MlflowAdapter(http=client)
        res_500 = await adapter.sync(source, credential=None, since=None)
        assert res_500.error is None
        # At least one version was fetch_failed due to the 500 error
        assert any(v.evaluation == "fetch_failed" for v in res_500.versions)

    # 401 on artifact call causes whole-sync error
    state.fail_next["/get-artifact"] = 401
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        adapter = MlflowAdapter(http=client)
        res_401 = await adapter.sync(source, credential=None, since=None)
        assert res_401.error == "The tracker rejected the credential"


@pytest.mark.asyncio
async def test_adapter_version_window_and_alias_filter() -> None:
    state = FakeMlflowState()
    app = create_fake_mlflow_app(state)

    t_now = 1700000000000
    state.add_experiment("Default")

    # Old version without alias (10 days old)
    t_old = t_now - 10 * 86400 * 1000
    state.add_run(experiment_id="0", run_id="r_old", start_time=t_old)
    state.register_version("m1", "1", run_id="r_old", creation_timestamp=t_old)
    state.put_artifact("r_old", "mlops_agents/evaluation.json", b'{"schema_version": 1}')

    # Old version WITH alias (10 days old)
    state.add_run(experiment_id="0", run_id="r_alias", start_time=t_old)
    state.register_version("m1", "2", run_id="r_alias", creation_timestamp=t_old)
    state.set_alias("m1", "prod", "2")
    state.put_artifact("r_alias", "mlops_agents/evaluation.json", b'{"schema_version": 1}')

    # Recent version (2 days old)
    t_recent = t_now - 2 * 86400 * 1000
    state.add_run(experiment_id="0", run_id="r_recent", start_time=t_recent)
    state.register_version("m1", "3", run_id="r_recent", creation_timestamp=t_recent)
    state.put_artifact("r_recent", "mlops_agents/evaluation.json", b'{"schema_version": 1}')

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        adapter = MlflowAdapter(http=client)
        source = _make_source_view()

        since_dt = datetime.fromtimestamp(t_now / 1000.0, tz=UTC)
        result = await adapter.sync(source, credential=None, since=since_dt)

        assert result.error is None
        returned_versions = {v.version for v in result.versions}
        # Version 1 is old and has no alias, so it is NOT returned
        assert "1" not in returned_versions
        # Version 2 is old but holds alias "prod", so it IS returned
        assert "2" in returned_versions
        # Version 3 is within the 7-day window, so it IS returned
        assert "3" in returned_versions


@pytest.mark.asyncio
async def test_adapter_first_sync_150_versions() -> None:
    state = FakeMlflowState()
    app = create_fake_mlflow_app(state)

    state.add_experiment("Default")
    t0_ms = 1700000000000
    for i in range(150):
        t_ms = t0_ms + i * 1000
        state.add_run(experiment_id="0", run_id=f"r_{i}", start_time=t_ms)
        state.register_version("large_model", str(i + 1), run_id=f"r_{i}", creation_timestamp=t_ms)
        state.put_artifact(f"r_{i}", "mlops_agents/evaluation.json", b'{"schema_version": 1}')

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        adapter = MlflowAdapter(http=client)
        source = _make_source_view()

        # First sync (since=None) fetches all 150 versions
        res1 = await adapter.sync(source, credential=None, since=None)
        assert res1.error is None
        assert len(res1.versions) == 150
        assert len(res1.evaluations) == 150

        # Later sync with since set after all versions, and only version 1 has alias
        state.set_alias("large_model", "prod", "1")
        since_future = datetime.fromtimestamp(t0_ms / 1000.0, tz=UTC) + timedelta(days=30)
        res2 = await adapter.sync(source, credential=None, since=since_future)
        assert res2.error is None
        # Only the alias holder version "1" is returned!
        assert len(res2.versions) == 1
        assert res2.versions[0].version == "1"


@pytest.mark.asyncio
async def test_e2e_tracking_service_with_fake_mlflow(tracking_env: TrackingEnv) -> None:
    state = FakeMlflowState()
    app = create_fake_mlflow_app(state)

    state.add_experiment("Default")
    t0_ms = 1700000000000
    state.add_run(experiment_id="0", run_id="run_1", start_time=t0_ms)
    state.register_version("ModelA", "1", run_id="run_1", creation_timestamp=t0_ms)
    state.set_alias("ModelA", "champion", "1")
    state.put_artifact(
        "run_1", "mlops_agents/evaluation.json", b'{"schema_version": 1, "accuracy": 0.88}'
    )

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # Use real MlflowAdapter in tracking service
        real_adapter = MlflowAdapter(http=client)
        service = TrackingService(
            databases=tracking_env.databases,
            keystore=tracking_env.keystore,
            secrets=tracking_env.secrets,
            events=tracking_env.events,
            adapters={"mlflow": real_adapter},
            github_token=tracking_env.get_github_token,
            clock=tracking_env.clock,
        )

        source_view = await service.create_source(
            tracking_env.project_id,
            type="mlflow",
            settings={"url": "http://testserver"},
            auth="none",
        )

        # 1. First sync fills record
        await service.run_sync(tracking_env.project_id, source_view.id)

        # Check evaluations page
        eval_page = await service.query(
            tracking_env.project_id,
            TrackingQuery(what="evaluations", model="ModelA", version="1"),
        )
        assert eval_page.total == 1
        assert eval_page.rows[0].payload["accuracy"] == 0.88

        # 2. Add version 2, set alias to version 2 -> second sync adds deployment
        t1_ms = t0_ms + 10000
        state.add_run(experiment_id="0", run_id="run_2", start_time=t1_ms)
        state.register_version("ModelA", "2", run_id="run_2", creation_timestamp=t1_ms)
        state.set_alias("ModelA", "champion", "2")
        state.put_artifact(
            "run_2", "mlops_agents/evaluation.json", b'{"schema_version": 1, "accuracy": 0.94}'
        )

        await service.run_sync(tracking_env.project_id, source_view.id)

        dep_page = await service.query(
            tracking_env.project_id,
            TrackingQuery(what="deployments", model="ModelA"),
        )
        assert dep_page.total == 2
        # Latest deployment
        assert dep_page.rows[0].from_version == "1"
        assert dep_page.rows[0].to_version == "2"

        # 3. Third sync with nothing changed adds none (idempotence)
        await service.run_sync(tracking_env.project_id, source_view.id)
        dep_page_3 = await service.query(
            tracking_env.project_id,
            TrackingQuery(what="deployments", model="ModelA"),
        )
        assert dep_page_3.total == 2

        # 4. Edited artifact updates evaluation in place
        state.put_artifact(
            "run_2", "mlops_agents/evaluation.json", b'{"schema_version": 1, "accuracy": 0.99}'
        )
        await service.run_sync(tracking_env.project_id, source_view.id)
        eval_page_v2 = await service.query(
            tracking_env.project_id,
            TrackingQuery(what="evaluations", model="ModelA", version="2"),
        )
        assert eval_page_v2.total == 1
        assert eval_page_v2.rows[0].payload["accuracy"] == 0.99

        # 5. Server error mid-test keeps last good data and shows error
        state.fail_next["experiments/search"] = 503
        await service.run_sync(tracking_env.project_id, source_view.id)
        sources = await service.sources(tracking_env.project_id)
        src = next(s for s in sources if s.id == source_view.id)
        assert src.last_error == "MLflow answered with HTTP 503"

        # Check data still intact
        dep_page_after_err = await service.query(
            tracking_env.project_id,
            TrackingQuery(what="deployments", model="ModelA"),
        )
        assert dep_page_after_err.total == 2
