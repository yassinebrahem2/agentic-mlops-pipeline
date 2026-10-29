from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx2
import pytest
from sqlalchemy import insert

from mlops_agents.agents.pr_sync import PrSyncObserver
from mlops_agents.agents.reviews import get_github_activity
from mlops_agents.agents.tasks.service import TasksService
from mlops_agents.clock import now_iso
from mlops_agents.integrations.github import api
from mlops_agents.integrations.github.auth import GITHUB_KEYSTORE_REF, GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import integrations
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


async def setup_sync_test(
    tmp_path: Path,
    git_env: dict[str, str],
    events: FakeEventLog | None = None,
) -> tuple[PrSyncObserver, TasksService, str, Path, httpx2.AsyncClient]:
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
    events = events or FakeEventLog()

    tasks = TasksService(
        databases=databases,
        paths=paths,
        git=runner,
        events=events,
        workspace=workspace,
    )

    observer = PrSyncObserver(tasks, github, http, events)
    return observer, tasks, project.id, repo, http


async def test_pr_sync_merged_pr_ends_task_and_removes_worktree(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    with git_instead_of(tmp_path, bare):
        observer, tasks, project_id, _, http = await setup_sync_test(tmp_path, git_env)
        try:
            session_id = "ses_1"
            paths = DataPaths(tmp_path / "home")
            databases = Databases(paths)
            await create_session(databases, project_id, session_id, agent="coder")

            task = await tasks.start(project_id, session_id, "coder", "Feature Task")
            worktree_path = task.worktree
            assert worktree_path.exists()

            await tasks.set_review(
                project_id,
                task.id,
                kind="github",
                pr_number=10,
                pr_url="https://github.com/test-owner/test-repo/pull/10",
            )

            fake_github.state.prs[("test-owner/test-repo", 10)] = {
                "number": 10,
                "title": "Feature Task",
                "body": "Opened by coder",
                "html_url": "https://github.com/test-owner/test-repo/pull/10",
                "state": "open",
                "merged": False,
            }

            # First sync while PR is open: task stays in_review
            await observer.sync_now(project_id)
            view = await tasks.view(project_id, task.id)
            assert view.status == "in_review"
            assert worktree_path.exists()

            # PR merged on GitHub
            fake_github.state.merge_pull("test-owner/test-repo", 10)

            # Sync again
            await observer.sync_now(project_id)

            view_after = await tasks.view(project_id, task.id)
            assert view_after.status == "merged"
            assert not worktree_path.exists()
        finally:
            await http.aclose()


async def test_pr_sync_closed_pr_ends_task_as_closed(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    with git_instead_of(tmp_path, bare):
        observer, tasks, project_id, _, http = await setup_sync_test(tmp_path, git_env)
        try:
            session_id = "ses_1"
            paths = DataPaths(tmp_path / "home")
            databases = Databases(paths)
            await create_session(databases, project_id, session_id, agent="coder")

            task = await tasks.start(project_id, session_id, "coder", "Abandoned Task")
            await tasks.set_review(
                project_id,
                task.id,
                kind="github",
                pr_number=11,
                pr_url="https://github.com/test-owner/test-repo/pull/11",
            )

            fake_github.state.prs[("test-owner/test-repo", 11)] = {
                "number": 11,
                "title": "Abandoned Task",
                "body": "Opened by coder",
                "html_url": "https://github.com/test-owner/test-repo/pull/11",
                "state": "open",
                "merged": False,
            }

            fake_github.state.close_pull("test-owner/test-repo", 11)

            await observer.sync_now(project_id)

            view = await tasks.view(project_id, task.id)
            assert view.status == "closed"
        finally:
            await http.aclose()


async def test_pr_sync_rate_limit_within_60_seconds(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    with git_instead_of(tmp_path, bare):
        observer, tasks, project_id, _, http = await setup_sync_test(tmp_path, git_env)
        try:
            session_id = "ses_1"
            paths = DataPaths(tmp_path / "home")
            databases = Databases(paths)
            await create_session(databases, project_id, session_id, agent="coder")

            task = await tasks.start(project_id, session_id, "coder", "Rate Limit Task")
            await tasks.set_review(
                project_id,
                task.id,
                kind="github",
                pr_number=12,
                pr_url="https://github.com/test-owner/test-repo/pull/12",
            )
            fake_github.state.prs[("test-owner/test-repo", 12)] = {
                "number": 12,
                "title": "Rate Limit Task",
                "body": "body",
                "html_url": "https://github.com/test-owner/test-repo/pull/12",
                "state": "open",
                "merged": False,
            }

            # Turn 1 started -> triggers background sync
            await observer.turn_started(project_id, "coder", "trn_1")
            await observer.wait_active()
            req_count_1 = len(fake_github.state.requests)
            assert req_count_1 > 0

            # Turn 2 started immediately (< 60s) -> should be rate limited, NO new requests
            await observer.turn_started(project_id, "coder", "trn_2")
            await observer.wait_active()
            req_count_2 = len(fake_github.state.requests)
            assert req_count_2 == req_count_1

            # Simulate 61 seconds passing
            observer._last_sync[project_id] -= 61.0  # pyright: ignore[reportPrivateUsage]

            # Turn 3 started -> sync runs again
            await observer.turn_started(project_id, "coder", "trn_3")
            await observer.wait_active()
            req_count_3 = len(fake_github.state.requests)
            assert req_count_3 > req_count_2
        finally:
            await http.aclose()


async def test_pr_sync_github_error_resilience(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    bare = make_bare_remote(tmp_path, git_env)
    with git_instead_of(tmp_path, bare):
        observer, tasks, project_id, _, http = await setup_sync_test(tmp_path, git_env)
        try:
            session_id = "ses_1"
            paths = DataPaths(tmp_path / "home")
            databases = Databases(paths)
            await create_session(databases, project_id, session_id, agent="coder")

            task = await tasks.start(project_id, session_id, "coder", "Error Task")
            await tasks.set_review(
                project_id,
                task.id,
                kind="github",
                pr_number=99,
                pr_url="https://github.com/test-owner/test-repo/pull/99",
            )

            # PR does not exist in fake_github (will return 404)
            # Calling sync_now should log warning and not raise
            await observer.sync_now(project_id)

            view = await tasks.view(project_id, task.id)
            # Task remains in review
            assert view.status == "in_review"
        finally:
            await http.aclose()


async def test_pr_sync_records_issue_updated_when_an_issue_closes_on_github(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    events = FakeEventLog()
    observer, tasks, project_id, _, http = await setup_sync_test(tmp_path, git_env, events)
    try:
        repo = "test-owner/test-repo"
        for number in (5, 6):
            await events.record(
                project_id,
                source="coder",
                type="issue.opened",
                payload={
                    "number": number,
                    "url": f"https://github.com/{repo}/issues/{number}",
                    "title": f"Issue {number}",
                    "state": "open",
                },
                subject_id=str(number),
            )
        # Issue 5 was closed on GitHub (by hand or by a merged PR); issue 6 no longer exists.
        fake_github.state.issues[(repo, 5)] = {
            "number": 5,
            "title": "Issue 5",
            "body": None,
            "html_url": f"https://github.com/{repo}/issues/5",
            "state": "closed",
            "labels": [],
        }

        await observer.sync_now(project_id)

        updates = await events.history(project_id, types=["issue.updated"])
        assert len(updates) == 1
        assert updates[0].payload == {
            "number": 5,
            "url": f"https://github.com/{repo}/issues/5",
            "title": "Issue 5",
            "state": "closed",
        }
        activity = await get_github_activity(events, tasks, project_id)
        states = {item.number: item.state for item in activity if item.kind == "issue"}
        assert states == {5: "closed", 6: "open"}
        # The item still belongs to the agent that opened the issue, not to the sync.
        assert {i.agent for i in activity if i.kind == "issue"} == {"coder"}

        # Nothing changed on GitHub, so the next sync records nothing.
        await observer.sync_now(project_id)
        assert len(await events.history(project_id, types=["issue.updated"])) == 1
    finally:
        await http.aclose()


async def _record_opened(events: FakeEventLog, project_id: str, repo: str, number: int) -> None:
    await events.record(
        project_id,
        source="coder",
        type="issue.opened",
        payload={
            "number": number,
            "url": f"https://github.com/{repo}/issues/{number}",
            "title": f"Issue {number}",
            "state": "open",
        },
        subject_id=str(number),
    )


def _closed_issue(repo: str, number: int) -> dict[str, object]:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": None,
        "html_url": f"https://github.com/{repo}/issues/{number}",
        "state": "closed",
        "labels": [],
    }


async def test_pr_sync_skips_a_deleted_issue_quietly_and_refreshes_the_next(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    events = FakeEventLog()
    observer, _, project_id, _, http = await setup_sync_test(tmp_path, git_env, events)
    try:
        repo = "test-owner/test-repo"
        # The deleted issue comes first, so the refresh must go on past it.
        for number in (7, 8):
            await _record_opened(events, project_id, repo, number)
        fake_github.state.delete_issue(repo, 7)
        fake_github.state.issues[(repo, 8)] = _closed_issue(repo, 8)

        with caplog.at_level(logging.DEBUG, logger="mlops_agents.agents.pr_sync"):
            await observer.sync_now(project_id)

        updates = await events.history(project_id, types=["issue.updated"])
        assert [u.payload["number"] for u in updates] == [8]
        sync_records = [r for r in caplog.records if r.name == "mlops_agents.agents.pr_sync"]
        assert [r for r in sync_records if r.levelno >= logging.WARNING] == []
        assert any("#7" in r.getMessage() for r in sync_records if r.levelno == logging.DEBUG)
    finally:
        await http.aclose()


async def test_pr_sync_refreshes_the_next_issue_after_a_transport_error(
    tmp_path: Path,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    events = FakeEventLog()
    observer, _, project_id, _, http = await setup_sync_test(tmp_path, git_env, events)
    original_send = http.send
    failed: list[str] = []

    async def send(request: httpx2.Request, **kwargs: Any) -> httpx2.Response:
        if request.url.path.endswith("/issues/9"):
            failed.append(request.url.path)
            raise httpx2.ConnectError("connection reset", request=request)
        return await original_send(request, **kwargs)

    try:
        repo = "test-owner/test-repo"
        for number in (9, 10):
            await _record_opened(events, project_id, repo, number)
        fake_github.state.issues[(repo, 10)] = _closed_issue(repo, 10)

        with patch.object(http, "send", send):
            await observer.sync_now(project_id)

        updates = await events.history(project_id, types=["issue.updated"])
        assert failed == ["/repos/test-owner/test-repo/issues/9"]
        assert [u.payload["number"] for u in updates] == [10]
    finally:
        await http.aclose()
