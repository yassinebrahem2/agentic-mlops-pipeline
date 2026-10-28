"""Tests for the fake MLflow server."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest

if TYPE_CHECKING:
    from fake_mlflow import FakeMlflowState, create_fake_mlflow_app
else:
    from tests.fakes.fake_mlflow import FakeMlflowState, create_fake_mlflow_app


@pytest.mark.asyncio
async def test_fake_mlflow_paging() -> None:
    state = FakeMlflowState(page_size=2)
    app = create_fake_mlflow_app(state)

    for i in range(5):
        state.add_experiment(f"exp_{i}")

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # Paging experiments
        seen_experiments: list[str] = []
        page_token: str | None = None
        while True:
            body: dict[str, object] = {"max_results": 2}
            if page_token is not None:
                body["page_token"] = page_token
            res = await client.post("/api/2.0/mlflow/experiments/search", json=body)
            assert res.status_code == 200
            data = res.json()
            for exp in data.get("experiments", []):
                seen_experiments.append(exp["name"])
            page_token = data.get("next_page_token")
            if not page_token:
                break
        assert seen_experiments == [f"exp_{i}" for i in range(5)]

        # Paging runs (newest first)
        for i in range(5):
            state.add_run(experiment_id="0", run_id=f"run_{i}", start_time=1000 + i * 100)

        seen_runs: list[str] = []
        page_token = None
        while True:
            body = {"experiment_ids": ["0"], "max_results": 2}
            if page_token is not None:
                body["page_token"] = page_token
            res = await client.post("/api/2.0/mlflow/runs/search", json=body)
            assert res.status_code == 200
            data = res.json()
            for run in data.get("runs", []):
                seen_runs.append(run["info"]["run_id"])
            page_token = data.get("next_page_token")
            if not page_token:
                break
        assert seen_runs == [f"run_{i}" for i in reversed(range(5))]

        # Paging registered models
        for i in range(5):
            state.register_version(f"model_{i}", "1")

        seen_models: list[str] = []
        page_token = None
        while True:
            params: dict[str, str | int] = {"max_results": 2}
            if page_token is not None:
                params["page_token"] = page_token
            res = await client.get("/api/2.0/mlflow/registered-models/search", params=params)
            assert res.status_code == 200
            data = res.json()
            for m in data.get("registered_models", []):
                seen_models.append(m["name"])
            page_token = data.get("next_page_token")
            if not page_token:
                break
        assert len(seen_models) == 5
        assert set(seen_models) == {f"model_{i}" for i in range(5)}

        # Paging model versions
        for v in range(5):
            state.register_version("paged_model", str(v + 1))

        seen_versions: list[str] = []
        page_token = None
        while True:
            params = {"filter": "name='paged_model'", "max_results": 2}
            if page_token is not None:
                params["page_token"] = page_token
            res = await client.get("/api/2.0/mlflow/model-versions/search", params=params)
            assert res.status_code == 200
            data = res.json()
            for mv in data.get("model_versions", []):
                seen_versions.append(mv["version"])
            page_token = data.get("next_page_token")
            if not page_token:
                break
        assert seen_versions == [str(v + 1) for v in range(5)]

        # Paging metric history
        for step in range(5):
            state.log_metric("run_0", "val_loss", float(step), step=step, timestamp=1000 + step)

        seen_steps: list[int] = []
        page_token = None
        while True:
            params = {"run_id": "run_0", "metric_key": "val_loss", "max_results": 2}
            if page_token is not None:
                params["page_token"] = page_token
            res = await client.get("/api/2.0/mlflow/metrics/get-history", params=params)
            assert res.status_code == 200
            data = res.json()
            for pt in data.get("metrics", []):
                seen_steps.append(pt["step"])
            page_token = data.get("next_page_token")
            if not page_token:
                break
        assert seen_steps == list(range(5))


@pytest.mark.asyncio
async def test_fake_mlflow_bearer_auth() -> None:
    state = FakeMlflowState(required_token="test-token")
    app = create_fake_mlflow_app(state)

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        res = await client.post("/api/2.0/mlflow/experiments/search", json={})
        assert res.status_code == 401
        assert res.json().get("error_code") == "UNAUTHENTICATED"

        res_bad = await client.post(
            "/api/2.0/mlflow/experiments/search",
            headers={"Authorization": "Bearer wrong-token"},
            json={},
        )
        assert res_bad.status_code == 401

        res_ok = await client.post(
            "/api/2.0/mlflow/experiments/search",
            headers={"Authorization": "Bearer test-token"},
            json={},
        )
        assert res_ok.status_code == 200


@pytest.mark.asyncio
async def test_fake_mlflow_fail_next() -> None:
    state = FakeMlflowState()
    state.fail_next["experiments/search"] = 503
    app = create_fake_mlflow_app(state)

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        res1 = await client.post("/api/2.0/mlflow/experiments/search", json={})
        assert res1.status_code == 503

        res2 = await client.post("/api/2.0/mlflow/experiments/search", json={})
        assert res2.status_code == 200


@pytest.mark.asyncio
async def test_fake_mlflow_aliases_supported_flag() -> None:
    state = FakeMlflowState(aliases_supported=False)
    state.register_version("my_model", "1")
    state.set_alias("my_model", "champion", "1")
    app = create_fake_mlflow_app(state)

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        res = await client.get("/api/2.0/mlflow/registered-models/search")
        assert res.status_code == 200
        models = res.json().get("registered_models", [])
        assert len(models) == 1
        assert "aliases" not in models[0]


@pytest.mark.asyncio
async def test_fake_mlflow_alias_endpoint_agrees_with_search() -> None:
    state = FakeMlflowState(aliases_supported=True)
    state.register_version("my_model", "1")
    state.register_version("my_model", "2")
    state.set_alias("my_model", "champion", "2")
    app = create_fake_mlflow_app(state)

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # Search returns alias list
        res_search = await client.get("/api/2.0/mlflow/registered-models/search")
        assert res_search.status_code == 200
        models = res_search.json().get("registered_models", [])
        assert len(models) == 1
        assert models[0]["aliases"] == [{"alias": "champion", "version": "2"}]

        # Alias endpoint agrees
        res_alias = await client.get(
            "/api/2.0/mlflow/registered-models/alias",
            params={"name": "my_model", "alias": "champion"},
        )
        assert res_alias.status_code == 200
        mv = res_alias.json().get("model_version", {})
        assert mv["name"] == "my_model"
        assert mv["version"] == "2"

        # Missing alias 404
        res_missing = await client.get(
            "/api/2.0/mlflow/registered-models/alias",
            params={"name": "my_model", "alias": "nonexistent"},
        )
        assert res_missing.status_code == 404
        assert res_missing.json().get("error_code") == "RESOURCE_DOES_NOT_EXIST"


@pytest.mark.asyncio
async def test_fake_mlflow_artifact_and_mutators() -> None:
    state = FakeMlflowState()
    state.add_run(experiment_id="0", run_id="run_art")
    state.put_artifact("run_art", "mlops_agents/evaluation.json", b'{"schema_version": 1}')
    app = create_fake_mlflow_app(state)

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        res = await client.get(
            "/get-artifact",
            params={"run_uuid": "run_art", "path": "mlops_agents/evaluation.json"},
        )
        assert res.status_code == 200
        assert res.content == b'{"schema_version": 1}'

        res_404 = await client.get(
            "/get-artifact",
            params={"run_uuid": "run_art", "path": "nonexistent.json"},
        )
        assert res_404.status_code == 404
