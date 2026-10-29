"""Tests for monitoring settings routes."""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.monitoring.settings import MonitoringSettings
from tests.api.conftest import add_local_project


@pytest.mark.asyncio
async def test_anonymous_cannot_access_monitoring_settings(anonymous: httpx2.AsyncClient) -> None:
    assert (await anonymous.get("/api/projects/prj_dummy/monitoring-settings")).status_code == 401
    assert (
        await anonymous.put(
            "/api/projects/prj_dummy/monitoring-settings",
            json=MonitoringSettings().model_dump(),
        )
    ).status_code == 401


@pytest.mark.asyncio
async def test_monitoring_settings_unknown_project_returns_404(client: httpx2.AsyncClient) -> None:
    get_res = await client.get("/api/projects/prj_unknown/monitoring-settings")
    assert get_res.status_code == 404
    assert get_res.json()["code"] == "project_not_found"

    put_res = await client.put(
        "/api/projects/prj_unknown/monitoring-settings",
        json=MonitoringSettings().model_dump(),
    )
    assert put_res.status_code == 404
    assert put_res.json()["code"] == "project_not_found"


@pytest.mark.asyncio
async def test_monitoring_settings_default(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    res = await client.get(f"/api/projects/{project_id}/monitoring-settings")
    assert res.status_code == 200
    expected = MonitoringSettings().model_dump()
    assert res.json() == expected


@pytest.mark.asyncio
async def test_monitoring_settings_round_trip(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    custom = MonitoringSettings(
        collection_interval_s=120,
        grouping_window_minutes=90,
        reopen_window_hours=48,
        timezone="UTC",
    ).model_dump()

    put_res = await client.put(
        f"/api/projects/{project_id}/monitoring-settings",
        json=custom,
    )
    assert put_res.status_code == 200
    assert put_res.json() == custom

    get_res = await client.get(f"/api/projects/{project_id}/monitoring-settings")
    assert get_res.status_code == 200
    assert get_res.json() == custom


@pytest.mark.asyncio
async def test_monitoring_settings_invalid_field_returns_422(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    payload = MonitoringSettings().model_dump()
    payload["collection_interval_s"] = 5  # must be at least 10

    res = await client.put(
        f"/api/projects/{project_id}/monitoring-settings",
        json=payload,
    )
    assert res.status_code == 422
    body = res.json()
    assert body["code"] == "invalid_request"
    errors = body["detail"]["errors"]
    assert any("collection_interval_s" in e["field"] for e in errors)


@pytest.mark.asyncio
async def test_monitoring_settings_extra_fields_returns_422(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    payload = MonitoringSettings().model_dump()
    payload["extra_field"] = "not_allowed"

    res = await client.put(
        f"/api/projects/{project_id}/monitoring-settings",
        json=payload,
    )
    assert res.status_code == 422
