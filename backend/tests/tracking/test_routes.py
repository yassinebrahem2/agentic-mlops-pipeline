"""Tests for tracking API routes: status codes, validation, 202 sync, absorption."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.tracking.contract import (
    ModelVersionRow,
    SyncResult,
)
from tests.api.conftest import LOCAL_URL, add_local_project, signed_in, started_app
from tests.tracking.conftest import FakeTrackingAdapter


@pytest.fixture
async def tracking_app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    async with started_app(tmp_path / "data") as app:
        yield app


@pytest.fixture
async def client(tracking_app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with signed_in(tracking_app, LOCAL_URL) as http:
        yield http


@pytest.fixture
async def tracking_setup(
    tracking_app: FastAPI, tmp_path: Path, git_env: dict[str, str]
) -> tuple[str, FakeTrackingAdapter]:
    project_id = await add_local_project(tracking_app, tmp_path, git_env)
    fake_adapter = FakeTrackingAdapter("mlflow", delay_s=0.01)
    tracking_app.state.services.tracking._adapters["mlflow"] = fake_adapter
    return project_id, fake_adapter


async def test_sources_routes_crud_and_credential(
    client: httpx2.AsyncClient,
    tracking_setup: tuple[str, FakeTrackingAdapter],
) -> None:
    prj, _ = tracking_setup

    # 1. Create source POST /api/projects/{id}/tracking/sources
    res = await client.post(
        f"/api/projects/{prj}/tracking/sources",
        json={
            "type": "mlflow",
            "settings": {"url": "https://mlflow.example.com"},
            "auth": "credential",
        },
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["type"] == "mlflow"
    assert data["credential_set"] is False
    source_id = data["id"]

    # 2. Get sources GET /api/projects/{id}/tracking/sources
    res = await client.get(f"/api/projects/{prj}/tracking/sources")
    assert res.status_code == 200
    sources = res.json()
    assert len(sources) == 1
    assert sources[0]["id"] == source_id

    # 3. Set credential PUT /api/projects/{id}/tracking/sources/{id}/credential
    res = await client.put(
        f"/api/projects/{prj}/tracking/sources/{source_id}/credential",
        json={"credential": "my-secret-token"},
    )
    assert res.status_code == 200
    cred_data = res.json()
    assert cred_data["credential_set"] is True
    assert "my-secret-token" not in res.text

    # 4. Empty credential is rejected with 422
    res = await client.put(
        f"/api/projects/{prj}/tracking/sources/{source_id}/credential",
        json={"credential": ""},
    )
    assert res.status_code == 422

    # 5. Unknown project -> 404
    res = await client.get("/api/projects/prj_unknown/tracking/sources")
    assert res.status_code == 404

    # 6. Bad source settings -> 422, secret values NOT echoed
    res = await client.post(
        f"/api/projects/{prj}/tracking/sources",
        json={
            "type": "mlflow",
            "settings": {"url": "https://user:pasted_super_secret_123@mlflow.example.com"},
            "auth": "none",
        },
    )
    assert res.status_code == 422
    assert "pasted_super_secret_123" not in res.text

    res = await client.post(
        f"/api/projects/{prj}/tracking/sources",
        json={
            "type": "mlflow",
            "settings": {
                "url": "https://mlflow.example.com",
                "token": "pasted_super_secret_456",
            },
            "auth": "none",
        },
    )
    assert res.status_code == 422
    assert "pasted_super_secret_456" not in res.text

    res = await client.post(
        f"/api/projects/{prj}/tracking/sources",
        json={
            "type": "mlflow",
            "settings": {
                "url": "https://mlflow.example.com?api_key=pasted_api_key_secret_789",
            },
            "auth": "none",
        },
    )
    assert res.status_code == 422
    assert "pasted_api_key_secret_789" not in res.text


async def test_sync_unknown_source_id_returns_404(
    client: httpx2.AsyncClient,
    tracking_setup: tuple[str, FakeTrackingAdapter],
) -> None:
    prj, _ = tracking_setup
    res = await client.post(
        f"/api/projects/{prj}/tracking/sync",
        json={"source_id": "trk_does_not_exist"},
    )
    assert res.status_code == 404
    assert res.json()["code"] == "tracking_source_not_found"


async def test_sync_route_answers_202_and_absorbs(
    client: httpx2.AsyncClient,
    tracking_app: FastAPI,
    tracking_setup: tuple[str, FakeTrackingAdapter],
) -> None:
    prj, adapter = tracking_setup

    # Create source
    res = await client.post(
        f"/api/projects/{prj}/tracking/sources",
        json={"type": "mlflow", "settings": {"url": "https://mlflow.example.com"}, "auth": "none"},
    )
    assert res.status_code == 200
    source_id = res.json()["id"]

    adapter.calls.clear()
    sync_entered = asyncio.Event()
    sync_proceed = asyncio.Event()

    async def blocking_sync(source: Any, credential: Any, since: Any) -> SyncResult:
        adapter.calls.append({"source": source})
        sync_entered.set()
        await sync_proceed.wait()
        return SyncResult(
            versions=[], runs=[], metrics=[], aliases={}, ci_runs=[], evaluations=[], error=None
        )

    adapter.sync = blocking_sync  # type: ignore[method-assign]

    # Two concurrent posts
    post1 = asyncio.create_task(
        client.post(f"/api/projects/{prj}/tracking/sync", json={"source_id": source_id})
    )
    await sync_entered.wait()

    post2 = asyncio.create_task(
        client.post(f"/api/projects/{prj}/tracking/sync", json={"source_id": source_id})
    )

    res1 = await post1
    res2 = await post2
    assert res1.status_code == 202
    assert res2.status_code == 202
    assert res1.text == ""
    assert res2.text == ""

    # Unblock first sync and wait for event
    sync_proceed.set()

    events_svc = tracking_app.state.services.events
    for _ in range(50):
        evts = await events_svc.history(prj, types=["tracking.synced"])
        if len(evts) == 1:
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("tracking.synced event was not recorded in time")

    assert len(adapter.calls) == 1


async def test_query_routes_and_filters(
    client: httpx2.AsyncClient,
    tracking_app: FastAPI,
    tracking_setup: tuple[str, FakeTrackingAdapter],
) -> None:
    prj, adapter = tracking_setup

    # Create source 1 and source 2
    res1 = await client.post(
        f"/api/projects/{prj}/tracking/sources",
        json={"type": "mlflow", "settings": {"url": "https://mlflow1.example.com"}, "auth": "none"},
    )
    s1 = res1.json()["id"]

    res2 = await client.post(
        f"/api/projects/{prj}/tracking/sources",
        json={"type": "mlflow", "settings": {"url": "https://mlflow2.example.com"}, "auth": "none"},
    )
    s2 = res2.json()["id"]

    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
    adapter.result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=s1,
                model="model_a",
                version="1",
                run_key="run_1",
                aliases=[],
                created=t0,
                evaluation="present",
            )
        ],
        runs=[],
        metrics=[],
        aliases={"model_a": {"production": "1"}},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await tracking_app.state.services.tracking.run_sync(prj, s1)

    # 1. Models GET
    res = await client.get(f"/api/projects/{prj}/tracking/models")
    assert res.status_code == 200
    assert "rows" in res.json()
    assert len(res.json()["rows"]) == 1

    # 2. Versions GET with source_id filter
    res = await client.get(f"/api/projects/{prj}/tracking/versions?source_id={s1}")
    assert res.status_code == 200
    assert len(res.json()["rows"]) == 1

    res = await client.get(f"/api/projects/{prj}/tracking/versions?source_id={s2}")
    assert res.status_code == 200
    assert len(res.json()["rows"]) == 0

    # 3. Deployments GET with source_id filter
    res = await client.get(f"/api/projects/{prj}/tracking/deployments?source_id={s1}")
    assert res.status_code == 200
    assert len(res.json()["rows"]) == 1

    res = await client.get(f"/api/projects/{prj}/tracking/deployments?source_id={s2}")
    assert res.status_code == 200
    assert len(res.json()["rows"]) == 0

    # 4. Runs GET
    res = await client.get(f"/api/projects/{prj}/tracking/runs")
    assert res.status_code == 200

    # 5. CI Runs GET
    res = await client.get(f"/api/projects/{prj}/tracking/ci-runs")
    assert res.status_code == 200

    # 6. Evaluations GET
    res = await client.get(f"/api/projects/{prj}/tracking/evaluations")
    assert res.status_code == 200

    # 7. Metrics without run_key -> 422
    res = await client.get(f"/api/projects/{prj}/tracking/metrics")
    assert res.status_code == 422
    assert res.json()["code"] == "tracking_bad_query"

    # 8. Limit > 500 -> 422 invalid_request
    res = await client.get(f"/api/projects/{prj}/tracking/models?limit=501")
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_request"

    # 9. Offset < 0 -> 422 invalid_request
    res = await client.get(f"/api/projects/{prj}/tracking/models?offset=-1")
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_request"


async def test_build_services_wiring(tracking_app: FastAPI) -> None:
    services = tracking_app.state.services
    assert services.tracking is not None
    assert callable(services.tracking.sources)
