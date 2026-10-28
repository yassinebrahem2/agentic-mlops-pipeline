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
from tests.conftest import MemoryKeyStore, make_repo, memory_project_secrets
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


ORIGIN = "https://github.com/test-owner/test-repo.git"


@contextmanager
def git_instead_of(
    tmp_path: Path, bare: Path, remote_url: str = ORIGIN
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
                .values(status="in_review", review_kind=kind, pr_number=pr_number, pr_url=pr_url)
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
    remote_url: str | None = None,
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
    if remote_url:
        cmd(["git", "remote", "add", "origin", remote_url], cwd=repo)
    http = httpx2.AsyncClient()
    keystore.set(GITHUB_KEYSTORE_REF, effective_token)
    app_engine = await databases.app()
    async with app_engine.begin() as conn:
        await conn.execute(
            insert(integrations).values(
                name="github", mode="token", login="octo", verified_at=now_iso()
            )
        )
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    runner = GitRunner(paths.bin, github.token)
    workspace = WorkspaceService(
        settings(paths), paths, databases, runner, github, http, memory_project_secrets(databases)
    )
    project = await workspace.add_local(str(repo))
    events = FakeEventLog()
    service = _TasksServiceForTests(
        databases=databases, paths=paths, git=runner, events=events, workspace=workspace
    )
    tool = OpenPullRequestTool(
        tasks=service, workspace=workspace, github=github, events=events, http=http
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
    service, _, events, tool, project_id, _ = await setup_test_service(
        tmp_path, paths, databases, keystore, git_env, remote_url=ORIGIN
    )
    with git_instead_of(tmp_path, bare):
        await create_session(databases, project_id, "ses_1", agent="coder")
        task = await service.start(project_id, "ses_1", "coder", "First PR task")

        (task.worktree / "feature.py").write_text("print('feature 1')\n", encoding="utf-8")
        ctx = await make_ctx(service, project_id, task)
        out1 = await tool.invoke(
            ctx, {"title": "Add feature", "body": "Summary of changes", "fixes": [3]}
        )
        assert out1.is_error is False
        assert "Pull request #1 opened:" in out1.content

        task_view = await service.view(project_id, task.id)
        assert task_view.status == "in_review"
        assert task_view.pr_number == 1

        pr_events = [r for r in events.records if r["type"].startswith("pr.")]
        assert len(pr_events) == 1
        assert pr_events[0]["payload"] == {
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

        pr_events = [r for r in events.records if r["type"].startswith("pr.")]
        assert len(pr_events) == 2
        assert pr_events[1]["payload"] == {
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
    service, _, events, tool, project_id, _ = await setup_test_service(
        tmp_path, paths, databases, keystore, git_env, remote_url=ORIGIN
    )
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


async def test_open_pull_request_push_failure_recovery_on_clean_tree(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    service, _, _, tool, project_id, _ = await setup_test_service(
        tmp_path, paths, databases, keystore, git_env, remote_url=ORIGIN
    )
    with git_instead_of(tmp_path, bare):
        await create_session(databases, project_id, "ses_1", agent="coder")
        task = await service.start(project_id, "ses_1", "coder", "Retry push task")
        (task.worktree / "work.py").write_text("print('push retry')\n", encoding="utf-8")
        ctx = await make_ctx(service, project_id, task)

        real_push = service.push
        call_count = 0

        async def failing_push(pid: str, t: TaskRef) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                from mlops_agents.integrations.github.git import GitError

                raise GitError("fatal: connection reset by peer", code=1)
            await real_push(pid, t)

        service.push = failing_push  # type: ignore[method-assign]

        out1 = await tool.invoke(ctx, {"title": "Retry PR", "body": "Initial body"})
        assert out1.is_error is True
        assert "fatal: connection reset" in out1.content

        # Tree is clean; second call publishes unpushed commits and creates PR
        out2 = await tool.invoke(ctx, {"title": "Retry PR", "body": "Initial body"})
        assert out2.is_error is False
        assert "Pull request #1 opened:" in out2.content


async def test_open_pull_request_clean_tree_update(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    service, _, _, tool, project_id, _ = await setup_test_service(
        tmp_path, paths, databases, keystore, git_env, remote_url=ORIGIN
    )
    with git_instead_of(tmp_path, bare):
        await create_session(databases, project_id, "ses_1", agent="coder")
        task = await service.start(project_id, "ses_1", "coder", "Clean update task")
        (task.worktree / "work.py").write_text("print('clean update')\n", encoding="utf-8")
        ctx = await make_ctx(service, project_id, task)

        out1 = await tool.invoke(ctx, {"title": "Initial Title", "body": "Initial Body"})
        assert out1.is_error is False

        # Tree is clean; call updates PR title and body on existing PR
        out2 = await tool.invoke(ctx, {"title": "Updated Title", "body": "Updated Body"})
        assert out2.is_error is False
        assert "Pull request #1 updated:" in out2.content

        pr_item = fake_github.state.prs.get(("test-owner/test-repo", 1))
        assert pr_item is not None
        assert pr_item["title"] == "Updated Title"
        assert "Updated Body" in pr_item["body"]


async def test_open_pull_request_create_pull_failure_recovery_on_clean_tree(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    service, _, events, tool, project_id, _ = await setup_test_service(
        tmp_path, paths, databases, keystore, git_env, remote_url=ORIGIN
    )
    with git_instead_of(tmp_path, bare):
        await create_session(databases, project_id, "ses_1", agent="coder")
        task = await service.start(project_id, "ses_1", "coder", "Retry create_pull task")
        (task.worktree / "work.py").write_text("print('create_pull retry')\n", encoding="utf-8")
        ctx = await make_ctx(service, project_id, task)

        # Simulate permission error on first create_pull call
        fake_github.state.simulate_permission_error["pull requests: write"] = 403

        out1 = await tool.invoke(ctx, {"title": "Retry PR", "body": "Initial body"})
        assert out1.is_error is True
        assert "403" in out1.content or "permission" in out1.content.lower()
        assert len([r for r in events.records if r["type"].startswith("pr.")]) == 0

        task_view = await service.view(project_id, task.id)
        assert task_view.pr_number is None

        # Tree is clean and push already succeeded on first call, but branch has commits beyond base
        fake_github.state.simulate_permission_error.clear()
        out2 = await tool.invoke(ctx, {"title": "Retry PR", "body": "Initial body"})
        assert out2.is_error is False
        assert "Pull request #1 opened:" in out2.content
        assert len([r for r in events.records if r["type"].startswith("pr.")]) == 1

        task_view = await service.view(project_id, task.id)
        assert task_view.status == "in_review"
        assert task_view.pr_number == 1


async def test_open_pull_request_zero_commits_no_pr_errors(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    service, _, events, tool, project_id, _ = await setup_test_service(
        tmp_path, paths, databases, keystore, git_env, remote_url=ORIGIN
    )
    with git_instead_of(tmp_path, bare):
        await create_session(databases, project_id, "ses_1", agent="coder")
        task = await service.start(project_id, "ses_1", "coder", "Zero commits task")
        ctx = await make_ctx(service, project_id, task)

        # Tree is clean, no commits beyond base, no PR
        out = await tool.invoke(ctx, {"title": "Empty PR", "body": "Empty body"})
        assert out.is_error is True
        assert "Nothing to commit" in out.content

        task_view = await service.view(project_id, task.id)
        assert task_view.pr_number is None
        assert len(fake_github.state.prs) == 0
        assert len([r for r in events.records if r["type"].startswith("pr.")]) == 0


async def test_open_pull_request_zero_commits_local_review_errors(
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
    task = await service.start(project_id, "ses_1", "coder", "Zero commits local task")
    ctx = await make_ctx(service, project_id, task)

    # Tree is clean, no commits beyond base
    out = await tool.invoke(ctx, {"title": "Empty local PR", "body": "Empty body"})
    assert out.is_error is True
    assert "Nothing to commit" in out.content

    task_view = await service.view(project_id, task.id)
    assert task_view.status != "in_review"
    assert len([r for r in events.records if r["type"].startswith("pr.")]) == 0


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
    workspace = WorkspaceService(
        settings(paths), paths, databases, runner, github, http, memory_project_secrets(databases)
    )
    events = FakeEventLog()
    service = _TasksServiceForTests(
        databases=databases, paths=paths, git=runner, events=events, workspace=workspace
    )
    reg_full = default_registry(
        tasks=service, workspace=workspace, github=github, events=events, http=http
    )
    assert "OpenPullRequest" in reg_full.names()
