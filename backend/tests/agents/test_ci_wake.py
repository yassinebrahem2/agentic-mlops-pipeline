from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from sqlalchemy import insert, select

from mlops_agents.agents import pr_sync
from mlops_agents.agents.pr_sync import PrSyncObserver
from mlops_agents.agents.tasks.service import TasksService
from mlops_agents.agents.tools.pull_request import OpenPullRequestTool
from mlops_agents.clock import now_iso
from mlops_agents.integrations.github import api
from mlops_agents.integrations.github.auth import GITHUB_KEYSTORE_REF, GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import integrations
from mlops_agents.storage.tables import tasks as tasks_table
from mlops_agents.workspace.service import WorkspaceService
from tests.agents.tasks.helpers import FakeEventLog, cmd, create_session, settings
from tests.agents.tools.test_pull_request import git_instead_of, make_bare_remote
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


class FakeSessions:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post(
        self,
        project_id: str,
        agent: str,
        *,
        author_name: str,
        from_agent: str,
        text: str,
        exchange_id: str | None = None,
        hop: int | None = None,
        system: bool = False,
    ) -> None:
        self.posts.append(
            {
                "project_id": project_id,
                "agent": agent,
                "author_name": author_name,
                "from_agent": from_agent,
                "text": text,
                "exchange_id": exchange_id,
                "hop": hop,
                "system": system,
            }
        )


async def get_ci_wake(databases: Databases, project_id: str, task_id: str) -> tuple[str | None, int]:
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(tasks_table.c.ci_woken_sha, tasks_table.c.ci_wakes).where(
                    tasks_table.c.id == task_id
                )
            )
        ).first()
        assert row is not None
        return row[0], int(row[1])


async def setup_ci_test(
    tmp_path: Path,
    git_env: dict[str, str],
) -> tuple[PrSyncObserver, TasksService, str, FakeSessions, WorkspaceService]:
    paths = DataPaths(tmp_path / "home")
    databases = Databases(paths)
    keystore = MemoryKeyStore()
    keystore.set(GITHUB_KEYSTORE_REF, "gho_test_token")

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

    repo = make_repo(tmp_path / "repo", {"app.py": "print('hello')\n"}, git_env)
    cmd(["git", "config", "user.name", "Jane Doe"], cwd=repo)
    cmd(["git", "config", "user.email", "jane@example.com"], cwd=repo)
    cmd(["git", "remote", "add", "origin", "https://github.com/test-owner/test-repo.git"], cwd=repo)

    http = httpx2.AsyncClient()
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    runner = GitRunner(paths.bin, github.token)
    workspace = WorkspaceService(
        settings(paths), paths, databases, runner, github, http, memory_project_secrets(databases)
    )
    project = await workspace.add_local(str(repo))
    events = FakeEventLog()

    tasks = TasksService(
        databases=databases,
        paths=paths,
        git=runner,
        events=events,
        workspace=workspace,
    )

    sessions = FakeSessions()
    observer = PrSyncObserver(
        tasks, github, http, events, sessions=cast(Any, sessions), workspace=workspace
    )
    return observer, tasks, project.id, sessions, workspace


def test_open_pull_request_tool_description() -> None:
    assert OpenPullRequestTool.description == (
        "Open a pull request on GitHub or create a local review for the current task. "
        "CI runs on the PR; if it fails, you are woken with the failing checks."
    )


async def test_red_pr_wakes_agent_once(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    with git_instead_of(tmp_path, bare):
        observer, tasks, project_id, sessions, _ = await setup_ci_test(tmp_path, git_env)
        session_id = "ses_1"
        paths = DataPaths(tmp_path / "home")
        databases = Databases(paths)
        await create_session(databases, project_id, session_id, agent="coder")

        task = await tasks.start(project_id, session_id, "coder", "Failing CI Task")
        await tasks.set_review(
            project_id,
            task.id,
            kind="github",
            pr_number=10,
            pr_url="https://github.com/test-owner/test-repo/pull/10",
        )

        fake_github.state.prs[("test-owner/test-repo", 10)] = {
            "number": 10,
            "title": "Failing CI Task",
            "body": "Opened by coder",
            "html_url": "https://github.com/test-owner/test-repo/pull/10",
            "state": "open",
            "merged": False,
            "head": {"sha": "sha_1", "ref": "branch"},
        }
        fake_github.state.check_runs[("test-owner/test-repo", "sha_1")] = [
            {
                "name": "Backend / tests",
                "status": "completed",
                "conclusion": "failure",
                "details_url": "https://github.com/test-owner/test-repo/actions/runs/101",
            }
        ]
        fake_github.state.check_runs[("test-owner/test-repo", "main")] = [
            {
                "name": "Backend / tests",
                "status": "completed",
                "conclusion": "success",
            }
        ]

        await observer.sync_now(project_id)

        assert len(sessions.posts) == 1
        post = sessions.posts[0]
        assert post["project_id"] == project_id
        assert post["agent"] == "coder"
        assert post["author_name"] == "system"
        assert post["from_agent"] == "system"
        assert post["system"] is True

        expected_msg = (
            f"CI failed on PR #10 for task {task.id}:\n"
            "- Backend / tests: failure (https://github.com/test-owner/test-repo/actions/runs/101)\n\n"
            f"Resume the task with ResumeTask {task.id}, reproduce by running the commands "
            "of the team's CI workflow in the sandbox, fix it and push with OpenPullRequest "
            "(it updates the same PR)."
        )
        assert post["text"] == expected_msg

        woken_sha, wakes = await get_ci_wake(databases, project_id, task.id)
        assert woken_sha == "sha_1"
        assert wakes == 1


async def test_same_sha_does_not_wake_again(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    with git_instead_of(tmp_path, bare):
        observer, tasks, project_id, sessions, _ = await setup_ci_test(tmp_path, git_env)
        session_id = "ses_1"
        paths = DataPaths(tmp_path / "home")
        databases = Databases(paths)
        await create_session(databases, project_id, session_id, agent="coder")

        task = await tasks.start(project_id, session_id, "coder", "Failing CI Task")
        await tasks.set_review(
            project_id,
            task.id,
            kind="github",
            pr_number=10,
            pr_url="https://github.com/test-owner/test-repo/pull/10",
        )

        fake_github.state.prs[("test-owner/test-repo", 10)] = {
            "number": 10,
            "title": "Failing CI Task",
            "body": "Opened by coder",
            "html_url": "https://github.com/test-owner/test-repo/pull/10",
            "state": "open",
            "merged": False,
            "head": {"sha": "sha_1", "ref": "branch"},
        }
        fake_github.state.check_runs[("test-owner/test-repo", "sha_1")] = [
            {
                "name": "Backend / tests",
                "status": "completed",
                "conclusion": "failure",
            }
        ]

        await observer.sync_now(project_id)
        assert len(sessions.posts) == 1

        # Second sync with same sha
        await observer.sync_now(project_id)
        assert len(sessions.posts) == 1

        woken_sha, wakes = await get_ci_wake(databases, project_id, task.id)
        assert wakes == 1
        assert woken_sha == "sha_1"


async def test_new_red_sha_wakes_again_up_to_3(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    with git_instead_of(tmp_path, bare):
        observer, tasks, project_id, sessions, _ = await setup_ci_test(tmp_path, git_env)
        session_id = "ses_1"
        paths = DataPaths(tmp_path / "home")
        databases = Databases(paths)
        await create_session(databases, project_id, session_id, agent="coder")

        task = await tasks.start(project_id, session_id, "coder", "Failing CI Task")
        await tasks.set_review(
            project_id,
            task.id,
            kind="github",
            pr_number=10,
            pr_url="https://github.com/test-owner/test-repo/pull/10",
        )

        pr = {
            "number": 10,
            "title": "Failing CI Task",
            "body": "Opened by coder",
            "html_url": "https://github.com/test-owner/test-repo/pull/10",
            "state": "open",
            "merged": False,
            "head": {"sha": "sha_1", "ref": "branch"},
        }
        fake_github.state.prs[("test-owner/test-repo", 10)] = pr

        # 1st wake
        fake_github.state.check_runs[("test-owner/test-repo", "sha_1")] = [
            {"name": "Backend / tests", "status": "completed", "conclusion": "failure"}
        ]
        await observer.sync_now(project_id)
        assert len(sessions.posts) == 1
        assert "This is the last CI wake" not in sessions.posts[0]["text"]

        # 2nd wake
        pr["head"] = {"sha": "sha_2", "ref": "branch"}
        fake_github.state.check_runs[("test-owner/test-repo", "sha_2")] = [
            {"name": "Backend / tests", "status": "completed", "conclusion": "failure"}
        ]
        await observer.sync_now(project_id)
        assert len(sessions.posts) == 2
        assert "This is the last CI wake" not in sessions.posts[1]["text"]

        # 3rd wake
        pr["head"] = {"sha": "sha_3", "ref": "branch"}
        fake_github.state.check_runs[("test-owner/test-repo", "sha_3")] = [
            {"name": "Backend / tests", "status": "completed", "conclusion": "failure"}
        ]
        await observer.sync_now(project_id)
        assert len(sessions.posts) == 3
        assert (
            "This is the last CI wake for this task: if it stays red, say so in your session and stop."
            in sessions.posts[2]["text"]
        )

        woken_sha, wakes = await get_ci_wake(databases, project_id, task.id)
        assert wakes == 3
        assert woken_sha == "sha_3"


async def test_wake_capped_at_3(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    with git_instead_of(tmp_path, bare):
        observer, tasks, project_id, sessions, _ = await setup_ci_test(tmp_path, git_env)
        session_id = "ses_1"
        paths = DataPaths(tmp_path / "home")
        databases = Databases(paths)
        await create_session(databases, project_id, session_id, agent="coder")

        task = await tasks.start(project_id, session_id, "coder", "Failing CI Task")
        await tasks.set_review(
            project_id,
            task.id,
            kind="github",
            pr_number=10,
            pr_url="https://github.com/test-owner/test-repo/pull/10",
        )

        pr = {
            "number": 10,
            "title": "Failing CI Task",
            "body": "Opened by coder",
            "html_url": "https://github.com/test-owner/test-repo/pull/10",
            "state": "open",
            "merged": False,
            "head": {"sha": "sha_1", "ref": "branch"},
        }
        fake_github.state.prs[("test-owner/test-repo", 10)] = pr

        for i in range(1, 4):
            sha = f"sha_{i}"
            pr["head"] = {"sha": sha, "ref": "branch"}
            fake_github.state.check_runs[("test-owner/test-repo", sha)] = [
                {"name": "Backend / tests", "status": "completed", "conclusion": "failure"}
            ]
            await observer.sync_now(project_id)

        assert len(sessions.posts) == 3

        # 4th failure on another sha
        pr["head"] = {"sha": "sha_4", "ref": "branch"}
        fake_github.state.check_runs[("test-owner/test-repo", "sha_4")] = [
            {"name": "Backend / tests", "status": "completed", "conclusion": "failure"}
        ]
        await observer.sync_now(project_id)

        # Still 3 posts, wakes remain 3
        assert len(sessions.posts) == 3
        woken_sha, wakes = await get_ci_wake(databases, project_id, task.id)
        assert wakes == 3
        assert woken_sha == "sha_3"


async def test_check_failing_on_default_branch_does_not_wake(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    with git_instead_of(tmp_path, bare):
        observer, tasks, project_id, sessions, _ = await setup_ci_test(tmp_path, git_env)
        session_id = "ses_1"
        paths = DataPaths(tmp_path / "home")
        databases = Databases(paths)
        await create_session(databases, project_id, session_id, agent="coder")

        task = await tasks.start(project_id, session_id, "coder", "Failing CI Task")
        await tasks.set_review(
            project_id,
            task.id,
            kind="github",
            pr_number=10,
            pr_url="https://github.com/test-owner/test-repo/pull/10",
        )

        fake_github.state.prs[("test-owner/test-repo", 10)] = {
            "number": 10,
            "title": "Failing CI Task",
            "body": "Opened by coder",
            "html_url": "https://github.com/test-owner/test-repo/pull/10",
            "state": "open",
            "merged": False,
            "head": {"sha": "sha_1", "ref": "branch"},
        }
        # Backend / tests failed on PR head, but ALSO failed on default branch (main)
        fake_github.state.check_runs[("test-owner/test-repo", "sha_1")] = [
            {
                "name": "Backend / tests",
                "status": "completed",
                "conclusion": "failure",
            }
        ]
        fake_github.state.check_runs[("test-owner/test-repo", "main")] = [
            {
                "name": "Backend / tests",
                "status": "completed",
                "conclusion": "failure",
            }
        ]

        await observer.sync_now(project_id)

        # Should not wake because the failure is also present on default branch
        assert len(sessions.posts) == 0
        view = await tasks.view(project_id, task.id)
        assert view.status == "in_review"
        woken_sha, wakes = await get_ci_wake(databases, project_id, task.id)
        assert wakes == 0
        assert woken_sha is None


async def test_pending_runs_do_not_wake(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    with git_instead_of(tmp_path, bare):
        observer, tasks, project_id, sessions, _ = await setup_ci_test(tmp_path, git_env)
        session_id = "ses_1"
        paths = DataPaths(tmp_path / "home")
        databases = Databases(paths)
        await create_session(databases, project_id, session_id, agent="coder")

        task = await tasks.start(project_id, session_id, "coder", "Failing CI Task")
        await tasks.set_review(
            project_id,
            task.id,
            kind="github",
            pr_number=10,
            pr_url="https://github.com/test-owner/test-repo/pull/10",
        )

        fake_github.state.prs[("test-owner/test-repo", 10)] = {
            "number": 10,
            "title": "Failing CI Task",
            "body": "Opened by coder",
            "html_url": "https://github.com/test-owner/test-repo/pull/10",
            "state": "open",
            "merged": False,
            "head": {"sha": "sha_1", "ref": "branch"},
        }
        fake_github.state.check_runs[("test-owner/test-repo", "sha_1")] = [
            {
                "name": "Backend / tests",
                "status": "completed",
                "conclusion": "failure",
            },
            {
                "name": "Frontend / tests",
                "status": "in_progress",
                "conclusion": None,
            },
        ]

        await observer.sync_now(project_id)

        # Should not wake because some runs are still in progress
        assert len(sessions.posts) == 0
        view = await tasks.view(project_id, task.id)
        assert view.status == "in_review"
        woken_sha, wakes = await get_ci_wake(databases, project_id, task.id)
        assert wakes == 0
        assert woken_sha is None


async def test_green_runs_do_not_wake(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    with git_instead_of(tmp_path, bare):
        observer, tasks, project_id, sessions, _ = await setup_ci_test(tmp_path, git_env)
        session_id = "ses_1"
        paths = DataPaths(tmp_path / "home")
        databases = Databases(paths)
        await create_session(databases, project_id, session_id, agent="coder")

        task = await tasks.start(project_id, session_id, "coder", "Passing CI Task")
        await tasks.set_review(
            project_id,
            task.id,
            kind="github",
            pr_number=10,
            pr_url="https://github.com/test-owner/test-repo/pull/10",
        )

        fake_github.state.prs[("test-owner/test-repo", 10)] = {
            "number": 10,
            "title": "Passing CI Task",
            "state": "open",
            "merged": False,
            "head": {"sha": "sha_1", "ref": "branch"},
        }
        fake_github.state.check_runs[("test-owner/test-repo", "sha_1")] = [
            {
                "name": "Backend / tests",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "Frontend / tests",
                "status": "completed",
                "conclusion": "skipped",
            },
            {
                "name": "Docs / build",
                "status": "completed",
                "conclusion": "neutral",
            },
        ]

        await observer.sync_now(project_id)

        # All conclusions in PASSING_CONCLUSIONS -> no wake
        assert len(sessions.posts) == 0
        view = await tasks.view(project_id, task.id)
        assert view.status == "in_review"
        woken_sha, wakes = await get_ci_wake(databases, project_id, task.id)
        assert wakes == 0
        assert woken_sha is None


async def test_concurrent_syncs_on_same_red_sha_post_once(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    with git_instead_of(tmp_path, bare):
        observer, tasks, project_id, sessions, _ = await setup_ci_test(tmp_path, git_env)
        session_id = "ses_1"
        paths = DataPaths(tmp_path / "home")
        databases = Databases(paths)
        await create_session(databases, project_id, session_id, agent="coder")

        task = await tasks.start(project_id, session_id, "coder", "Concurrent CI Task")
        await tasks.set_review(
            project_id,
            task.id,
            kind="github",
            pr_number=10,
            pr_url="https://github.com/test-owner/test-repo/pull/10",
        )

        fake_github.state.prs[("test-owner/test-repo", 10)] = {
            "number": 10,
            "title": "Concurrent CI Task",
            "body": "Opened by coder",
            "html_url": "https://github.com/test-owner/test-repo/pull/10",
            "state": "open",
            "merged": False,
            "head": {"sha": "sha_1", "ref": "branch"},
        }
        fake_github.state.check_runs[("test-owner/test-repo", "sha_1")] = [
            {
                "name": "Backend / tests",
                "status": "completed",
                "conclusion": "failure",
            }
        ]

        # Two concurrent syncs against the same red sha
        await asyncio.gather(observer.sync_now(project_id), observer.sync_now(project_id))

        assert len(sessions.posts) == 1
        woken_sha, wakes = await get_ci_wake(databases, project_id, task.id)
        assert wakes == 1
        assert woken_sha == "sha_1"


async def test_default_branch_lookup_failure_does_not_wake(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    with git_instead_of(tmp_path, bare):
        observer, tasks, project_id, sessions, _ = await setup_ci_test(tmp_path, git_env)
        session_id = "ses_1"
        paths = DataPaths(tmp_path / "home")
        databases = Databases(paths)
        await create_session(databases, project_id, session_id, agent="coder")

        task = await tasks.start(project_id, session_id, "coder", "Failing CI Task")
        await tasks.set_review(
            project_id,
            task.id,
            kind="github",
            pr_number=10,
            pr_url="https://github.com/test-owner/test-repo/pull/10",
        )

        fake_github.state.prs[("test-owner/test-repo", 10)] = {
            "number": 10,
            "title": "Failing CI Task",
            "body": "Opened by coder",
            "html_url": "https://github.com/test-owner/test-repo/pull/10",
            "state": "open",
            "merged": False,
            "head": {"sha": "sha_1", "ref": "branch"},
        }
        fake_github.state.check_runs[("test-owner/test-repo", "sha_1")] = [
            {"name": "Backend / tests", "status": "completed", "conclusion": "failure"}
        ]

        real_get_check_runs = pr_sync.get_check_runs

        async def fail_main_runs(http: Any, token: str, repo: str, ref: str) -> Any:
            if ref == "main":
                raise RuntimeError("GitHub check runs error")
            return await real_get_check_runs(http, token, repo, ref)

        monkeypatch.setattr(pr_sync, "get_check_runs", fail_main_runs)

        await observer.sync_now(project_id)

        # Lookup failure on default branch causes early return without waking or recording
        assert len(sessions.posts) == 0
        woken_sha, wakes = await get_ci_wake(databases, project_id, task.id)
        assert wakes == 0
        assert woken_sha is None


async def test_pr_with_no_check_runs_does_not_wake(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    with git_instead_of(tmp_path, bare):
        observer, tasks, project_id, sessions, _ = await setup_ci_test(tmp_path, git_env)
        session_id = "ses_1"
        paths = DataPaths(tmp_path / "home")
        databases = Databases(paths)
        await create_session(databases, project_id, session_id, agent="coder")

        task = await tasks.start(project_id, session_id, "coder", "No CI Task")
        await tasks.set_review(
            project_id,
            task.id,
            kind="github",
            pr_number=10,
            pr_url="https://github.com/test-owner/test-repo/pull/10",
        )

        fake_github.state.prs[("test-owner/test-repo", 10)] = {
            "number": 10,
            "title": "No CI Task",
            "state": "open",
            "merged": False,
            "head": {"sha": "sha_1", "ref": "branch"},
        }
        fake_github.state.check_runs[("test-owner/test-repo", "sha_1")] = []

        await observer.sync_now(project_id)

        assert len(sessions.posts) == 0
        woken_sha, wakes = await get_ci_wake(databases, project_id, task.id)
        assert wakes == 0
        assert woken_sha is None
