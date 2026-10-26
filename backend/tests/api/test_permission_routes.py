"""Tests for permission settings routes."""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.agents.policy.table import PermissionLevel
from tests.api.conftest import add_local_project


@pytest.mark.asyncio
async def test_anonymous_cannot_access_permissions(anonymous: httpx2.AsyncClient) -> None:
    assert (await anonymous.get("/api/projects/prj_dummy/settings/permissions")).status_code == 401
    assert (
        await anonymous.put(
            "/api/projects/prj_dummy/settings/permissions",
            json={"level": "autonomous"},
        )
    ).status_code == 401


@pytest.mark.asyncio
async def test_permissions_unknown_project_returns_404(client: httpx2.AsyncClient) -> None:
    get_res = await client.get("/api/projects/prj_unknown/settings/permissions")
    assert get_res.status_code == 404
    assert get_res.json()["code"] == "project_not_found"

    put_res = await client.put(
        "/api/projects/prj_unknown/settings/permissions",
        json={"level": "autonomous"},
    )
    assert put_res.status_code == 404
    assert put_res.json()["code"] == "project_not_found"


@pytest.mark.asyncio
async def test_permissions_default_is_ask(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    res = await client.get(f"/api/projects/{project_id}/settings/permissions")
    assert res.status_code == 200
    assert res.json() == {"level": "ask"}


@pytest.mark.asyncio
async def test_permissions_round_trip_all_levels(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    levels: list[PermissionLevel] = ["suggest", "ask", "autonomous", "bypass"]
    for level in levels:
        put_res = await client.put(
            f"/api/projects/{project_id}/settings/permissions",
            json={"level": level},
        )
        assert put_res.status_code == 200
        assert put_res.json() == {"level": level}

        get_res = await client.get(f"/api/projects/{project_id}/settings/permissions")
        assert get_res.status_code == 200
        assert get_res.json() == {"level": level}


@pytest.mark.asyncio
async def test_permissions_invalid_level_returns_422(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    res = await client.put(
        f"/api/projects/{project_id}/settings/permissions",
        json={"level": "nonexistent_level"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_permissions_extra_fields_returns_422(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    res = await client.put(
        f"/api/projects/{project_id}/settings/permissions",
        json={"level": "ask", "extra": "forbidden"},
    )
    assert res.status_code == 422
