from __future__ import annotations

import os
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import httpx2
import pytest
from sqlalchemy import insert, update

from mlops_agents.agents.runtime.contract import TaskView
from mlops_agents.agents.tasks.service import TasksService
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import TaskRef, ToolContext
from mlops_agents.agents.tools.pull_request import OpenPullRequestArgs, OpenPullRequestTool
from mlops_agents.clock import now_iso
from mlops_agents.integrations.github import api
from mlops_agents.integrations.github.auth import GITHUB_KEYSTORE_REF, GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import integrations, tasks
from mlops_agents.workspace.service import WorkspaceService
from tests.agents.ctx import tool_context
from tests.agents.tasks.helpers import FakeEventLog, cmd, create_session, settings
from tests.conftest import MemoryKeyStore, make_repo
from tests.fakes.fake_github import FakeGitHubState, RunningFakeGitHub, start_fake_github
from tests.integrations.test_github_auth import FakeGh


@pytest.fixture
def fake_github() -> Iterator[RunningFakeGitHub]:
    state = FakeGitHubState()
    running = start_fake_github(state)
    original_api = api.GITHUB_API
    api.GITHUB_API = running.base_url
    try:
        yield running
    finally:
        api.GITHUB_API = original_api
        running.stop()


@contextmanager
def git_instead_of(
    tmp_path: Path, bare: Path, remote_url: str = "https://github.com/test-owner/test-repo.git"
) -> Generator[None, None, None]:
    fake_global = tmp_path / f"fake_global_{bare.name}.gitconfig"
    cmd(["git", "config", "--file", str(fake_global), f"url.{bare.as_uri()}.insteadOf", remote_url])
    old_global = os.environ.get("GIT_CONFIG_GLOBAL")
    os.environ["GIT_CONFIG_GLOBAL"] = str(fake_global)
    try:
        yield
    finally:
        if old_global is None:
            os.environ.pop("GIT_CONFIG_GLOBAL", None)
        else:
            os.environ["GIT_CONFIG_GLOBAL"] = old_global


def make_bare_remote(tmp_path: Path, git_env: dict[str, str], name: str = "remote.git") -> Path:
    bare = tmp_path / name
    bare.mkdir(exist_ok=True)
    cmd(["git", "init", "--bare", str(bare)])
    seed = tmp_path / f"seed_{name}"
    make_repo(seed, {"remote_file.txt": "remote\n"}, git_env)
    cmd(["git", "push", str(bare), "main"], cwd=seed)
    return bare


class _TasksServiceForTests(TasksService):
    """Subclass implementing set_review for tests while PR #259 is pending merge."""

    __test__ = False

    async def set_review(
        self,
        project_id: str,
        task_id: str,
        *,
        kind: Literal["github", "local"],
        pr_number: int | None = None,
        pr_url: str | None = None,
    ) -> TaskView:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id)
                .values(
                    status="in_review",
                    review_kind=kind,
                    pr_number=pr_number,
                    pr_url=pr_url,
                )
            )
        return await self.view(project_id, task_id)


async def setup_test_service(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    *,
    token: str | None = None,
) -> tuple[
    _TasksServiceForTests,
    WorkspaceService,
    FakeEventLog,
    OpenPullRequestTool,
    str,
    Path,
]:
    effective_token = token if token is not None else "gho_test_token"
    repo = make_repo(tmp_path / "repo", {"app.py": "print('hello')\n"}, git_env)
    cmd(["git", "config", "user.name", "Jane Doe"], cwd=repo)
    cmd(["git", "config", "user.email", "jane@example.com"], cwd=repo)
    http = httpx2.AsyncClient()
    keystore.set(GITHUB_KEYSTORE_REF, effective_token)
    app_engine = await databases.app()
    async with app_engine.begin() as conn:
        await conn.execute(
            insert(integrations).values(
                name="github",
                mode="token",
                login="octo",
                verified_at=now_iso(),
            )
        )
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    runner = GitRunner(paths.bin, github.token)
    workspace = WorkspaceService(settings(paths), paths, databases, runner, github, http)
    project = await workspace.add_local(str(repo))
    events = FakeEventLog()
    service = _TasksServiceForTests(
        databases=databases,
        paths=paths,
        git=runner,
        events=events,
        workspace=workspace,
    )
    tool = OpenPullRequestTool(
        tasks=service,
        workspace=workspace,
        github=github,
        events=events,
        http=http,
    )
    return service, workspace, events, tool, project.id, repo


async def make_ctx(
    service: TasksService, project_id: str, task: TaskRef | None, session_id: str = "ses_1"
) -> ToolContext:
    files = await service.files(project_id, task)
    return tool_context(
        files, project_id=project_id, agent="coder", session_id=session_id, task=task
    )


async def test_open_pull_request_first_call_and_second_call(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    service, _, events, tool, project_id, repo = await setup_test_service(
        tmp_path, paths, databases, keystore, git_env
    )
    cmd(["git", "remote", "add", "origin", "https://github.com/test-owner/test-repo.git"], cwd=repo)

    with git_instead_of(tmp_path, bare):
        await create_session(databases, project_id, "ses_1", agent="coder")
        task = await service.start(project_id, "ses_1", "coder", "First PR task")

        # 1. First call: creates PR with footer and fixes
        (task.worktree / "feature.py").write_text("print('feature 1')\n", encoding="utf-8")
        ctx = await make_ctx(service, project_id, task)

        out1 = await tool.invoke(
            ctx, {"title": "Add feature", "body": "Summary of changes", "fixes": [3]}
        )
        assert out1.is_error is False
        assert "Pull request #1 opened:" in out1.content
        assert "https://github.com/test-owner/test-repo/pull/1" in out1.content

        # Verify task is in_review on github
        task_view = await service.view(project_id, task.id)
        assert task_view.status == "in_review"
        assert task_view.review_kind == "github"
        assert task_view.pr_number == 1
        assert task_view.pr_url == "https://github.com/test-owner/test-repo/pull/1"

        # Verify event pr.opened recorded
        pr_events = [r for r in events.records if r["type"].startswith("pr.")]
        assert len(pr_events) == 1
        evt1 = pr_events[0]
        assert evt1["type"] == "pr.opened"
        assert evt1["source"] == "coder"
        assert evt1["subject_id"] == task.id
        assert evt1["payload"] == {
            "task_id": task.id,
            "number": 1,
            "url": "https://github.com/test-owner/test-repo/pull/1",
            "title": "Add feature",
        }

        # 2. Second call after a change: updates existing PR
        (task.worktree / "feature.py").write_text("print('feature 1 updated')\n", encoding="utf-8")
        out2 = await tool.invoke(
            ctx, {"title": "Add feature (updated)", "body": "Updated summary", "fixes": [3, 4]}
        )
        assert out2.is_error is False
        assert "Pull request #1 updated:" in out2.content

        # Verify event pr.updated recorded
        pr_events = [r for r in events.records if r["type"].startswith("pr.")]
        assert len(pr_events) == 2
        evt2 = pr_events[1]
        assert evt2["type"] == "pr.updated"
        assert evt2["source"] == "coder"
        assert evt2["subject_id"] == task.id
        assert evt2["payload"] == {
            "task_id": task.id,
            "number": 1,
            "url": "https://github.com/test-owner/test-repo/pull/1",
            "title": "Add feature (updated)",
        }


async def test_open_pull_request_denylisted_file(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    service, _, events, tool, project_id, repo = await setup_test_service(
        tmp_path, paths, databases, keystore, git_env
    )
    cmd(["git", "remote", "add", "origin", "https://github.com/test-owner/test-repo.git"], cwd=repo)

    with git_instead_of(tmp_path, bare):
        await create_session(databases, project_id, "ses_1", agent="coder")
        task = await service.start(project_id, "ses_1", "coder", "Denylisted task")

        (task.worktree / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
        ctx = await make_ctx(service, project_id, task)

        out = await tool.invoke(ctx, {"title": "Secret PR", "body": "Body"})
        assert out.is_error is True
        assert ".env" in out.content
        assert len([r for r in events.records if r["type"].startswith("pr.")]) == 0


async def test_open_pull_request_no_repo_ref_local_review(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, events, tool, project_id, _repo = await setup_test_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Local review task")

    (task.worktree / "local_change.py").write_text("print('local')\n", encoding="utf-8")
    ctx = await make_ctx(service, project_id, task)

    out = await tool.invoke(ctx, {"title": "Local review PR", "body": "Local body"})
    assert out.is_error is False
    assert out.content == "Local review opened for task 'Local review task'."

    task_view = await service.view(project_id, task.id)
    assert task_view.status == "in_review"
    assert task_view.review_kind == "local"
    assert task_view.pr_number is None
    assert len([r for r in events.records if r["type"].startswith("pr.")]) == 0


async def test_open_pull_request_no_current_task(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, _, tool, project_id, _repo = await setup_test_service(
        tmp_path, paths, databases, keystore, git_env
    )
    ctx = await make_ctx(service, project_id, None)

    out = await tool.invoke(ctx, {"title": "No task", "body": "No task body"})
    assert out.is_error is True
    assert out.content == "Start a task first with StartTask."


async def test_open_pull_request_preview(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, _, tool, project_id, _repo = await setup_test_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Preview task")

    (task.worktree / "preview_file.py").write_text("print('diff preview')\n", encoding="utf-8")
    ctx = await make_ctx(service, project_id, task)

    prev = await tool.preview(ctx, OpenPullRequestArgs(title="Preview title", body="Preview body"))
    assert prev.kind == "diff"
    assert prev.title == "Preview title"
    assert "preview_file.py" in prev.body
    assert "diff preview" in prev.body


def test_default_registry_wiring(
    databases: Databases,
    paths: DataPaths,
    keystore: MemoryKeyStore,
) -> None:
    reg_empty = default_registry()
    assert "OpenPullRequest" not in reg_empty.names()

    http = httpx2.AsyncClient()
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    runner = GitRunner(paths.bin, github.token)
    workspace = WorkspaceService(settings(paths), paths, databases, runner, github, http)
    events = FakeEventLog()
    service = _TasksServiceForTests(
        databases=databases,
        paths=paths,
        git=runner,
        events=events,
        workspace=workspace,
    )

    reg_full = default_registry(
        tasks=service,
        workspace=workspace,
        github=github,
        events=events,
        http=http,
    )
    assert "OpenPullRequest" in reg_full.names()
