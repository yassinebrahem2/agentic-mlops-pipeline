from __future__ import annotations

from pathlib import Path

import httpx2

from tests.conftest import make_repo


async def test_issues_settings_auth(anonymous: httpx2.AsyncClient) -> None:
    res = await anonymous.get("/api/projects/prj_123/settings/issues")
    assert res.status_code == 401

    res = await anonymous.put(
        "/api/projects/prj_123/settings/issues", json={"min_priority": "high"}
    )
    assert res.status_code == 401


async def test_issues_settings_unknown_project(client: httpx2.AsyncClient) -> None:
    res = await client.get("/api/projects/prj_unknown/settings/issues")
    assert res.status_code == 404

    res = await client.put(
        "/api/projects/prj_unknown/settings/issues", json={"min_priority": "high"}
    )
    assert res.status_code == 404


async def test_issues_settings_lifecycle(
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    repo = make_repo(tmp_path / "repo", {"README.md": "# Project\n"}, git_env)
    res = await client.post("/api/projects", json={"source": "local", "path": str(repo)})
    assert res.status_code == 201
    project_id = res.json()["id"]

    # Default min_priority is "medium"
    res = await client.get(f"/api/projects/{project_id}/settings/issues")
    assert res.status_code == 200
    assert res.json() == {"min_priority": "medium"}

    # Update to "high"
    res = await client.put(
        f"/api/projects/{project_id}/settings/issues", json={"min_priority": "high"}
    )
    assert res.status_code == 200
    assert res.json() == {"min_priority": "high"}

    # Verify GET returns "high"
    res = await client.get(f"/api/projects/{project_id}/settings/issues")
    assert res.status_code == 200
    assert res.json() == {"min_priority": "high"}

    # Update to "critical"
    res = await client.put(
        f"/api/projects/{project_id}/settings/issues", json={"min_priority": "critical"}
    )
    assert res.status_code == 200
    assert res.json() == {"min_priority": "critical"}

    # Update to "low"
    res = await client.put(
        f"/api/projects/{project_id}/settings/issues", json={"min_priority": "low"}
    )
    assert res.status_code == 200
    assert res.json() == {"min_priority": "low"}

    # Verify GET returns "low"
    res = await client.get(f"/api/projects/{project_id}/settings/issues")
    assert res.status_code == 200
    assert res.json() == {"min_priority": "low"}


async def test_issues_settings_invalid_body(
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    repo = make_repo(tmp_path / "repo", {}, git_env)
    res = await client.post("/api/projects", json={"source": "local", "path": str(repo)})
    assert res.status_code == 201
    project_id = res.json()["id"]

    # Invalid priority string
    res = await client.put(
        f"/api/projects/{project_id}/settings/issues", json={"min_priority": "urgent"}
    )
    assert res.status_code == 422

    # Missing field
    res = await client.put(f"/api/projects/{project_id}/settings/issues", json={})
    assert res.status_code == 422
