from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

import httpx2
from fastapi import FastAPI

from mlops_agents.config import APP_NAME
from mlops_agents.sandbox.docker import DockerResult, DockerRunner
from tests.conftest import make_repo


class FakeRebuildRunner(DockerRunner):
    def __init__(self) -> None:
        self.built_tags: set[str] = set()

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None = None,
        secrets: Iterable[str] = (),
    ) -> DockerResult:
        cmd = list(args)
        if cmd[0] == "version":
            import json

            data = {"Client": {"Version": "27.0.0"}, "Server": {"Version": "27.0.0"}}
            return DockerResult(stdout=json.dumps(data), stderr="", exit_code=0)

        if cmd[0] == "image" and cmd[1] == "inspect":
            tag = cmd[2]
            if tag in self.built_tags:
                return DockerResult(stdout="{}", stderr="", exit_code=0)
            return DockerResult(stdout="", stderr="no such image", exit_code=1)

        if cmd[0] == "build":
            tag_idx = cmd.index("-t") + 1
            tag = cmd[tag_idx]
            self.built_tags.add(tag)
            return DockerResult(stdout="Built", stderr="", exit_code=0)

        if cmd[0] == "images":
            return DockerResult(stdout="", stderr="", exit_code=0)

        if cmd[0] == "ps":
            return DockerResult(stdout="", stderr="", exit_code=0)

        return DockerResult(stdout="", stderr="", exit_code=0)


async def test_rebuild_requires_auth(anonymous: httpx2.AsyncClient) -> None:
    res = await anonymous.post("/api/projects/prj_123/sandbox/rebuild")
    assert res.status_code == 401


async def test_rebuild_unknown_project(client: httpx2.AsyncClient) -> None:
    res = await client.post("/api/projects/prj_unknown/sandbox/rebuild")
    assert res.status_code == 404


async def test_rebuild_success(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    runner = FakeRebuildRunner()
    # Inject fake runner into sandbox service
    app.state.services.sandbox._runner = runner

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
