"""Tests for GET /api/projects/{project_id}/expectations route."""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

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
    res = await client.get(f"/api/projects/{project_id}/expectations")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
