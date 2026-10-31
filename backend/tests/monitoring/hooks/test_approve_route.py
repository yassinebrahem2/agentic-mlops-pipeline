"""Tests for POST /api/projects/{project_id}/hooks/{hook_id}/approve route and tool non-reachability (Task 7, #492)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.proposals.payloads import CommandSpec
from tests.api.conftest import LOCAL_URL, add_local_project, signed_in, started_app


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    async with started_app(tmp_path / "data") as application:
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with signed_in(app, LOCAL_URL) as http:
        yield http


@pytest.mark.asyncio
async def test_approve_route_reactivates_hook_and_updates_pins(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services = app.state.services

    # Create a file in repo and commit it to main
    project = await services.workspace.get(project_id)
    repo_dir = services.workspace.repo_root(project)
    train_file = repo_dir / "train.py"
    train_file.write_text("print('v1')\n", encoding="utf-8")
    git = GitRunner(services.paths.bin)
    await git.run(["add", "train.py"], cwd=repo_dir, extra_env=git_env)
    await git.run(["commit", "-m", "add train.py"], cwd=repo_dir, extra_env=git_env)

    spec = CommandSpec(
        kind="train",
        type="command",
        template="python train.py",
        workflow=None,
        inputs={},
        placeholders=[],
        deploys=False,
        entry_files=["train.py"],
    )
    hook = await services.hooks.register(
        project_id,
        spec=spec,
        name="train-hook",
        model="iris",
        source="proposal",
    )

    # Suspend hook
    await services.hooks.suspend(project_id, hook.id, "tampered file")
    suspended = await services.hooks.get(project_id, hook.id)
    assert suspended is not None
    assert suspended.status == "suspended"
    assert suspended.suspended_reason == "tampered file"

    # POST /api/projects/{project_id}/hooks/{hook_id}/approve
    resp = await client.post(f"/api/projects/{project_id}/hooks/{hook.id}/approve")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == hook.id
    assert data["status"] == "active"
    assert data["suspended_reason"] is None
    assert data["approved_commit"] is not None
    assert len(data["pins"]) == 1
    assert data["pins"][0]["path"] == "train.py"

    # Verify state in service
    reloaded = await services.hooks.get(project_id, hook.id)
    assert reloaded is not None
    assert reloaded.status == "active"
    assert reloaded.suspended_reason is None
    assert reloaded.approved_commit is not None


@pytest.mark.asyncio
async def test_approve_route_unknown_hook_404(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    resp = await client.post(f"/api/projects/{project_id}/hooks/hok_nonexistent/approve")
    assert resp.status_code == 404
    data = resp.json()
    assert data["code"] == "hook_not_found"


@pytest.mark.asyncio
async def test_approve_hook_not_reachable_as_tool(
    app: FastAPI,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    await add_local_project(app, tmp_path, git_env)
    services = app.state.services

    tool_names = services.tools.names()
    assert "ApproveHook" not in tool_names
    assert "approve_hook" not in tool_names
    assert "Approve" not in tool_names

    # Approve route is a person-only action (session auth, never an agent tool)
    assert services.tools.get("ApproveHook") is None
