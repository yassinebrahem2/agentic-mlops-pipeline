from __future__ import annotations

from pathlib import Path

import httpx2

from mlops_agents.config import APP_NAME
from tests.conftest import make_repo


async def test_rebuild_requires_auth(anonymous: httpx2.AsyncClient) -> None:
    res = await anonymous.post("/api/projects/prj_123/sandbox/rebuild")
    assert res.status_code == 401


async def test_rebuild_unknown_project(client: httpx2.AsyncClient) -> None:
    res = await client.post("/api/projects/prj_unknown/sandbox/rebuild")
    assert res.status_code == 404


async def test_rebuild_success(
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    repo = make_repo(tmp_path / "repo", {"requirements.txt": "scikit-learn\n"}, git_env)
    res = await client.post("/api/projects", json={"source": "local", "path": str(repo)})
    assert res.status_code == 201
    project_id = res.json()["id"]

    # Initial sandbox status has state none
    res = await client.get(f"/api/projects/{project_id}/sandbox")
    assert res.status_code == 200
    assert res.json()["environment"]["state"] == "none"

    # Rebuild
    res = await client.post(f"/api/projects/{project_id}/sandbox/rebuild")
    assert res.status_code == 200
    env_data = res.json()
    assert env_data["state"] == "ready"
    assert env_data["tag"].startswith(f"{APP_NAME}-env:{project_id}-")
    assert env_data["detail"] is None

    # GET returns the updated status
    res = await client.get(f"/api/projects/{project_id}/sandbox")
    assert res.status_code == 200
    assert res.json()["environment"]["state"] == "ready"
    assert res.json()["environment"]["tag"] == env_data["tag"]
