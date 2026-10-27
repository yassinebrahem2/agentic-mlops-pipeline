from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.agents.tasks.git import run_safe_git
from mlops_agents.events.contract import EventLog
from mlops_agents.integrations.github.git import GitRunner
from tests.api.conftest import add_local_project


@pytest.fixture(autouse=True)
def _suppress_docker_cleanup() -> Any:
    with patch(
        "mlops_agents.sandbox.containers.ContainerManager.cleanup_all",
        new=AsyncMock(),
    ):
        yield


async def _setup_project(app: FastAPI, tmp_path: Path, git_env: dict[str, str]) -> str:
    project_id = await add_local_project(app, tmp_path, git_env)
    services = app.state.services
    project = await services.workspace.get(project_id)
    repo_root = services.workspace.repo_root(project)
    git = GitRunner(services.paths.bin, token_provider=services.github.token)
    await run_safe_git(
        git,
        args=["config", "user.name", "Test User"],
        cwd=repo_root,
        empty_hooks=services.paths.empty_hooks,
    )
    await run_safe_git(
        git,
        args=["config", "user.email", "test@example.com"],
        cwd=repo_root,
        empty_hooks=services.paths.empty_hooks,
    )
    return project_id


async def test_reviews_routes_auth(anonymous: httpx2.AsyncClient) -> None:
    res = await anonymous.get("/api/projects/prj_123/reviews")
    assert res.status_code == 401

    res = await anonymous.post(
        "/api/projects/prj_123/reviews/tsk_123/decision",
        json={"approve": True},
    )
    assert res.status_code == 401

    res = await anonymous.get("/api/projects/prj_123/github/activity")
    assert res.status_code == 401


async def test_reviews_routes_unknown_project(client: httpx2.AsyncClient) -> None:
    res = await client.get("/api/projects/prj_unknown/reviews")
    assert res.status_code == 404

    res = await client.post(
        "/api/projects/prj_unknown/reviews/tsk_123/decision",
        json={"approve": True},
    )
    assert res.status_code == 404

    res = await client.get("/api/projects/prj_unknown/github/activity")
    assert res.status_code == 404


async def test_reviews_routes_lifecycle(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await _setup_project(app, tmp_path, git_env)
    services = app.state.services
    session = await services.sessions.get(project_id, "orchestrator")

    # Start task, edit file, commit, and set local review
    task = await services.tasks.start(project_id, session.id, "orchestrator", "Task 1")
    mod_file = task.worktree / "test_lifecycle.txt"
    mod_file.write_text("content\n", encoding="utf-8")
    await services.tasks.claim(project_id, task, ["test_lifecycle.txt"])
    await services.tasks.commit(project_id, task, "feat: test lifecycle")
    await services.tasks.set_review(project_id, task.id, kind="local")

    # GET /reviews
    res = await client.get(f"/api/projects/{project_id}/reviews")
    assert res.status_code == 200
    reviews = res.json()
    assert len(reviews) == 1
    review = reviews[0]
    assert review["task"]["id"] == task.id
    assert review["agent"] == "orchestrator"
    assert "test_lifecycle.txt" in review["files_changed"]
    assert "+content" in review["diff"]

    # POST /reviews/{task_id}/decision (approve)
    res = await client.post(
        f"/api/projects/{project_id}/reviews/{task.id}/decision",
        json={"approve": True},
    )
    assert res.status_code == 200
    task_view = res.json()
    assert task_view["status"] == "merged"

    # GET /reviews is now empty
    res = await client.get(f"/api/projects/{project_id}/reviews")
    assert res.status_code == 200
    assert res.json() == []


async def test_reviews_routes_conflict(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await _setup_project(app, tmp_path, git_env)
    services = app.state.services
    project = await services.workspace.get(project_id)
    repo_root = services.workspace.repo_root(project)
    session = await services.sessions.get(project_id, "orchestrator")

    # Start task and edit README.md
    task = await services.tasks.start(project_id, session.id, "orchestrator", "Task Conflict")
    (task.worktree / "README.md").write_text("# Task\n", encoding="utf-8")
    await services.tasks.claim(project_id, task, ["README.md"])
    await services.tasks.commit(project_id, task, "change readme")
    await services.tasks.set_review(project_id, task.id, kind="local")

    # Conflicting edit on main
    (repo_root / "README.md").write_text("# Main\n", encoding="utf-8")
    git = GitRunner(services.paths.bin, token_provider=services.github.token)
    await run_safe_git(
        git,
        args=["commit", "-am", "conflict commit"],
        cwd=repo_root,
        empty_hooks=services.paths.empty_hooks,
    )

    # POST /reviews/{task_id}/decision -> 409 Conflict
    res = await client.post(
        f"/api/projects/{project_id}/reviews/{task.id}/decision",
        json={"approve": True},
    )
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == "conflict"

    # GET /reviews still returns the review
    res = await client.get(f"/api/projects/{project_id}/reviews")
    assert res.status_code == 200
    assert len(res.json()) == 1


async def test_reviews_routes_reject(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await _setup_project(app, tmp_path, git_env)
    services = app.state.services
    session = await services.sessions.get(project_id, "orchestrator")

    task = await services.tasks.start(project_id, session.id, "orchestrator", "Task Reject")
    await services.tasks.set_review(project_id, task.id, kind="local")

    res = await client.post(
        f"/api/projects/{project_id}/reviews/{task.id}/decision",
        json={"approve": False, "note": "Please rework"},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "closed"

    # Verify session received note
    session_view = await services.sessions.get(project_id, "orchestrator")
    assert len(session_view.messages) > 0
    last_msg = session_view.messages[-1]
    assert last_msg.author_name == "Review"
    assert last_msg.from_agent == "orchestrator"
    msg_text = "".join(getattr(p, "text", "") for p in last_msg.parts)
    assert "Please rework" in msg_text


async def test_github_activity_route(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await _setup_project(app, tmp_path, git_env)
    services = app.state.services
    events: EventLog = services.events

    await events.record(
        project_id,
        source="orchestrator",
        type="issue.opened",
        payload={
            "number": 1,
            "title": "Issue #1",
            "url": "https://github.com/issues/1",
            "state": "open",
        },
        subject_id="1",
    )

    res = await client.get(f"/api/projects/{project_id}/github/activity")
    assert res.status_code == 200
    items = res.json()
    assert len(items) == 1
    assert items[0]["kind"] == "issue"
    assert items[0]["number"] == 1
    assert items[0]["title"] == "Issue #1"
