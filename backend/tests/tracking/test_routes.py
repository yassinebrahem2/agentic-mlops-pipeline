"""Tests for tracking API routes: status codes, validation, 202 sync, absorption."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

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
    fake_adapter = FakeTrackingAdapter("mlflow", delay_s=0.05)
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

    # 4. Unknown project -> 404
    res = await client.get("/api/projects/prj_unknown/tracking/sources")
    assert res.status_code == 404

    # 5. Bad source settings -> 422
    res = await client.post(
        f"/api/projects/{prj}/tracking/sources",
        json={"type": "mlflow", "settings": {"url": "file:///bad"}, "auth": "none"},
    )
    assert res.status_code == 422


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
    adapter.delay_s = 0.3

    # Two quick posts
    res1, res2 = await asyncio.gather(
        client.post(f"/api/projects/{prj}/tracking/sync", json={"source_id": source_id}),
        client.post(f"/api/projects/{prj}/tracking/sync", json={"source_id": source_id}),
    )
    assert res1.status_code == 202
    assert res2.status_code == 202
    assert res1.text == ""
    assert res2.text == ""

    # Allow background sync task to finish
    await asyncio.sleep(0.4)

    # Should run adapter only once due to absorption
    assert len(adapter.calls) == 1

    # Check event was recorded
    events_svc = tracking_app.state.services.events
    evts = await events_svc.history(prj, types=["tracking.synced"])
    assert len(evts) == 1


async def test_query_routes(
    client: httpx2.AsyncClient,
    tracking_setup: tuple[str, FakeTrackingAdapter],
) -> None:
    prj, _ = tracking_setup

    # 1. Models GET
    res = await client.get(f"/api/projects/{prj}/tracking/models")
    assert res.status_code == 200
    assert "rows" in res.json()

    # 2. Versions GET
    res = await client.get(f"/api/projects/{prj}/tracking/versions")
    assert res.status_code == 200

    # 3. Runs GET
    res = await client.get(f"/api/projects/{prj}/tracking/runs")
    assert res.status_code == 200

    # 4. CI Runs GET
    res = await client.get(f"/api/projects/{prj}/tracking/ci-runs")
    assert res.status_code == 200

    # 5. Deployments GET
    res = await client.get(f"/api/projects/{prj}/tracking/deployments")
    assert res.status_code == 200

    # 6. Evaluations GET
    res = await client.get(f"/api/projects/{prj}/tracking/evaluations")
    assert res.status_code == 200

    # 7. Metrics without run_key -> 422
    res = await client.get(f"/api/projects/{prj}/tracking/metrics")
    assert res.status_code == 422
    assert res.json()["code"] == "tracking_bad_query"

    # 8. Limit > 500 -> 422
    res = await client.get(f"/api/projects/{prj}/tracking/models?limit=501")
    assert res.status_code == 422
