from __future__ import annotations

from pathlib import Path

import httpx2

from tests.api.conftest import LOCAL_URL, signed_in, started_app
from tests.conftest import make_repo
from tests.sandbox.fakes import FakeDockerRunner


async def test_anonymous_cannot_access_sandbox(anonymous: httpx2.AsyncClient) -> None:
    res = await anonymous.get("/api/projects/prj_123/sandbox")
    assert res.status_code == 401


async def test_project_sandbox_status(
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    repo = make_repo(tmp_path / "repo", {"README.md": "# Sandbox\n"}, git_env)
    res = await client.post("/api/projects", json={"source": "local", "path": str(repo)})
    assert res.status_code == 201
    project_id = res.json()["id"]

    res = await client.get(f"/api/projects/{project_id}/sandbox")
    assert res.status_code == 200
    data = res.json()
    assert "docker" in data
    assert "environment" in data
    assert data["network"] is False
    assert "available" in data["docker"]
    assert data["environment"]["state"] == "none"


async def test_project_sandbox_unknown_project(client: httpx2.AsyncClient) -> None:
    res = await client.get("/api/projects/prj_unknown/sandbox")
    assert res.status_code == 404


async def test_sandbox_daemon_down_returns_200_with_unavailable_and_503_on_actions(
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    repo = make_repo(tmp_path / "repo", {"requirements.txt": "scikit-learn\n"}, git_env)
    home = tmp_path / "data"
    fake_docker = FakeDockerRunner(daemon_down=True)
    async with (
        started_app(home, docker_runner=fake_docker) as application,
        signed_in(application, LOCAL_URL) as auth_client,
    ):
        res = await auth_client.post("/api/projects", json={"source": "local", "path": str(repo)})
        assert res.status_code == 201
        project_id = res.json()["id"]

        # 1. GET /sandbox always answers 200 with docker.available False and environment unavailable
        res = await auth_client.get(f"/api/projects/{project_id}/sandbox")
        assert res.status_code == 200
        data = res.json()
        assert data["docker"]["available"] is False
        assert "Docker daemon is not running" in data["docker"]["detail"]
        assert data["environment"]["state"] == "unavailable"
        assert data["environment"]["tag"] is None
        assert "Docker daemon is not running" in (data["environment"]["detail"] or "")

        # 2. POST /sandbox/rebuild answers 503 sandbox_unavailable
        res = await auth_client.post(f"/api/projects/{project_id}/sandbox/rebuild")
        assert res.status_code == 503
        err_data = res.json()
        assert err_data["code"] == "sandbox_unavailable"
        assert "Docker daemon is not running" in err_data["message"]

        # 3. PUT /settings/sandbox answers 503 sandbox_unavailable
        res = await auth_client.put(
            f"/api/projects/{project_id}/settings/sandbox", json={"network": True}
        )
        assert res.status_code == 503
        err_data = res.json()
        assert err_data["code"] == "sandbox_unavailable"
        assert "Docker daemon is not running" in err_data["message"]
