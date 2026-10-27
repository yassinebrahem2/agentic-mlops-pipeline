"""A session's view carries its current task, read through `TaskWorkspaces.view` (#261)."""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import TaskView
from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import build_session_env
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.fakes.workspaces import FakeWorkspaces

TASK = TaskView(
    id="tsk_1",
    agent="orchestrator",
    title="Fix the readme",
    branch="task/fix-the-readme",
    status="active",
    review_kind=None,
    pr_number=None,
    pr_url=None,
)


async def test_the_session_view_carries_the_current_tasks_view(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    views: list[tuple[str, str]] = []

    async def current(self: FakeWorkspaces, project_id: str, session_id: str) -> TaskRef:
        return TaskRef(
            id=TASK.id, title=TASK.title, branch=TASK.branch, worktree=tmp_path, status="active"
        )

    async def view(self: FakeWorkspaces, project_id: str, task_id: str) -> TaskView:
        views.append((project_id, task_id))
        return TASK

    monkeypatch.setattr(FakeWorkspaces, "current", current)
    monkeypatch.setattr(FakeWorkspaces, "view", view)
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )

    session = await env.manager.get(env.project_id, "orchestrator")

    assert session.current_task == TASK
    assert views == [(env.project_id, TASK.id)]
    await env.manager.close()
