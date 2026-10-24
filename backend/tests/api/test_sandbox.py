from __future__ import annotations

from pathlib import Path

import httpx2

from mlops_agents.sandbox.docker import DockerResult, DockerRunner
from tests.conftest import make_repo


class FakeRunner(DockerRunner):
    async def run(
        self,
        args: object,
        *,
        timeout_s: float | None = None,
        secrets: object = (),
    ) -> DockerResult:
        import json

        data = {"Client": {"Version": "27.0.0"}, "Server": {"Version": "27.0.0"}}
        return DockerResult(stdout=json.dumps(data), stderr="", exit_code=0)


async def test_anonymous_cannot_access_sandbox(anonymous: httpx2.AsyncClient) -> None:
    res = await anonymous.get("/api/sandbox")
    assert res.status_code == 401

    res = await anonymous.get("/api/projects/prj_123/sandbox")
    assert res.status_code == 401


async def test_sandbox_global_status(client: httpx2.AsyncClient) -> None:
    res = await client.get("/api/sandbox")
    assert res.status_code == 200
    data = res.json()
    assert "docker" in data
    assert "environment" in data
    assert data["network"] is False
    assert "available" in data["docker"]
    assert data["environment"]["state"] == "none"


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
