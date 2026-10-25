from __future__ import annotations

from pathlib import Path

import httpx2
from fastapi import FastAPI

from tests.conftest import make_repo


async def test_sandbox_settings_auth(anonymous: httpx2.AsyncClient) -> None:
    res = await anonymous.put("/api/projects/prj_123/settings/sandbox", json={"network": True})
    assert res.status_code == 401


async def test_sandbox_settings_unknown_project(client: httpx2.AsyncClient) -> None:
    res = await client.put("/api/projects/prj_unknown/settings/sandbox", json={"network": True})
    assert res.status_code == 404


async def test_sandbox_settings_lifecycle(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    repo = make_repo(tmp_path / "repo", {"requirements.txt": "scikit-learn\n"}, git_env)
    res = await client.post("/api/projects", json={"source": "local", "path": str(repo)})
    assert res.status_code == 201
    project_id = res.json()["id"]

    # Default network is False
    res = await client.get(f"/api/projects/{project_id}/sandbox")
    assert res.status_code == 200
    assert res.json()["network"] is False

    # Enable network
    res = await client.put(f"/api/projects/{project_id}/settings/sandbox", json={"network": True})
    assert res.status_code == 200
    assert res.json() == {"network": True}

    # Verify GET returns updated network
    res = await client.get(f"/api/projects/{project_id}/sandbox")
    assert res.status_code == 200
    assert res.json()["network"] is True

    # Disable network
    res = await client.put(f"/api/projects/{project_id}/settings/sandbox", json={"network": False})
    assert res.status_code == 200
    assert res.json() == {"network": False}

    # Verify GET reflects False
    res = await client.get(f"/api/projects/{project_id}/sandbox")
    assert res.status_code == 200
    assert res.json()["network"] is False


async def test_sandbox_settings_invalid_body(
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    repo = make_repo(tmp_path / "repo", {}, git_env)
    res = await client.post("/api/projects", json={"source": "local", "path": str(repo)})
    assert res.status_code == 201
    project_id = res.json()["id"]

    res = await client.put(
        f"/api/projects/{project_id}/settings/sandbox", json={"network": "invalid"}
    )
    assert res.status_code == 422
