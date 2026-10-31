"""Tests for GET /api/projects/{project_id}/expectations route."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.monitoring.expectations.contract import ExpectationKey, ExpectationSpec
from mlops_agents.proposals.payloads import Cadence
from tests.api.conftest import add_local_project


@pytest.mark.asyncio
async def test_anonymous_cannot_access_expectations(anonymous: httpx2.AsyncClient) -> None:
    res = await anonymous.get("/api/projects/prj_dummy/expectations")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_expectations_unknown_project_returns_404(client: httpx2.AsyncClient) -> None:
    res = await client.get("/api/projects/prj_unknown/expectations")
    assert res.status_code == 404
    assert res.json()["code"] == "project_not_found"


@pytest.mark.asyncio
async def test_expectations_list_route(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)

    # Seed an expectation into the project's expectations store
    spec = ExpectationSpec(
        key=ExpectationKey(kind="feed", ref="daily_sales"),
        category="data_quality",
        cadence=Cadence(cron=None, every_minutes=60),
        timezone="UTC",
        grace_minutes=30,
        source="handoff",
    )
    await app.state.services.expectations.store.register(project_id, spec)

    res = await client.get(f"/api/projects/{project_id}/expectations")
    assert res.status_code == 200
    data = cast(list[dict[str, Any]], res.json())
    assert isinstance(data, list)
    assert len(data) == 1
    exp = data[0]
    assert exp["key"] == {"kind": "feed", "ref": "daily_sales", "model": None}
    assert exp["category"] == "data_quality"
    assert exp["active"] is True
    assert exp["status"] in {"ok", "due", "late", "silent"}
    assert "next_deadline" in exp
    assert exp["last_seen"] is None
