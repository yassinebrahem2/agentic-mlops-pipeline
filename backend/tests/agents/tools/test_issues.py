from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import insert

from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import ToolContext
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.agents.tools.issues import (
    CommentOnIssueTool,
    OpenIssueTool,
    SearchIssuesTool,
    get_issue_min_priority,
    set_issue_min_priority,
)
from mlops_agents.clock import now_iso
from mlops_agents.integrations.github import api
from mlops_agents.integrations.github.auth import GITHUB_KEYSTORE_REF, GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.integrations.github.issues import create_issue
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import integrations
from mlops_agents.workspace.service import WorkspaceService
from tests.agents.ctx import tool_context
from tests.agents.tasks.helpers import FakeEventLog, cmd, settings
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


@dataclass
class TestEnv:
    __test__ = False
    workspace: WorkspaceService
    github: GitHubAuth
    events: FakeEventLog
    http: httpx2.AsyncClient
    project_id: str
    repo: Path
    ctx: ToolContext


async def setup_test_env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    *,
    remote_url: str | None = "https://github.com/test-owner/test-repo.git",
    with_token: bool = True,
    agent: str = "tester",
) -> TestEnv:
    repo = make_repo(tmp_path / "repo", {"app.py": "print('hello')\n"}, git_env)
    if remote_url is not None:
        cmd(["git", "remote", "add", "origin", remote_url], cwd=repo)

    http = httpx2.AsyncClient()
    if with_token:
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

    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    runner = GitRunner(paths.bin, github.token)
    workspace = WorkspaceService(settings(paths), paths, databases, runner, github, http)
    project = await workspace.add_local(str(repo))
    events = FakeEventLog()
    files = RepoFiles(repo, GitRunner(paths.bin))
    ctx = tool_context(files, project_id=project.id, agent=agent)
    return TestEnv(workspace, github, events, http, project.id, repo, ctx)


async def test_search_issues_no_github_link(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    env = await setup_test_env(tmp_path, paths, databases, keystore, git_env, remote_url=None)
    tool = SearchIssuesTool(env.workspace, env.github, env.http)
    res = await tool.invoke(env.ctx, {"query": ""})
    assert res.is_error is True
    assert res.content == "Project has no GitHub link."


async def test_search_issues_no_token(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    env = await setup_test_env(tmp_path, paths, databases, keystore, git_env, with_token=False)
    tool = SearchIssuesTool(env.workspace, env.github, env.http)
    res = await tool.invoke(env.ctx, {"query": ""})
    assert res.is_error is True
    assert res.content == "GitHub integration is not configured."


async def test_search_issues_matching_and_empty(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    env = await setup_test_env(tmp_path, paths, databases, keystore, git_env)
    token = "gho_test_token"
    await create_issue(
        env.http,
        token,
        "test-owner/test-repo",
        title="Fix model performance drop",
        body="Observed accuracy decrease",
        labels=["priority:high", "category:model"],
    )
    await create_issue(
        env.http,
        token,
        "test-owner/test-repo",
        title="Add onboarding dataset",
        body="Need CSV parser",
        labels=["category:data"],
    )

    tool = SearchIssuesTool(env.workspace, env.github, env.http)
    res_all = await tool.invoke(env.ctx, {"query": ""})
    assert res_all.is_error is False
    assert "#1" in res_all.content and "Fix model performance drop" in res_all.content
    assert "#2" in res_all.content and "Add onboarding dataset" in res_all.content

    res_match = await tool.invoke(env.ctx, {"query": "model"})
    assert res_match.is_error is False
    assert "#1" in res_match.content and "#2" not in res_match.content

    res_none = await tool.invoke(env.ctx, {"query": "nonexistentxyz"})
    assert res_none.is_error is False
    assert "No open issues found." in res_none.content


async def test_open_issue_no_github_link(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    env = await setup_test_env(tmp_path, paths, databases, keystore, git_env, remote_url=None)
    tool = OpenIssueTool(env.workspace, env.github, env.events, env.http, databases)
    res = await tool.invoke(
        env.ctx,
        {"title": "Bug", "body": "Details", "category": "model", "priority": "high"},
    )
    assert res.is_error is True
    assert res.content == "Project has no GitHub link."


async def test_open_issue_no_token(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    env = await setup_test_env(tmp_path, paths, databases, keystore, git_env, with_token=False)
    tool = OpenIssueTool(env.workspace, env.github, env.events, env.http, databases)
    res = await tool.invoke(
        env.ctx,
        {"title": "Bug", "body": "Details", "category": "model", "priority": "high"},
    )
    assert res.is_error is True
    assert res.content == "GitHub integration is not configured."


async def test_open_issue_refused_below_minimum_priority(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    env = await setup_test_env(tmp_path, paths, databases, keystore, git_env)
    tool = OpenIssueTool(env.workspace, env.github, env.events, env.http, databases)
    res = await tool.invoke(
        env.ctx,
        {"title": "Tweak", "body": "Body", "category": "ui", "priority": "low"},
    )
    assert res.is_error is True
    assert (
        res.content
        == "Issue priority 'low' is below the project's minimum 'medium'. Report this in your session instead."
    )
    assert len(env.events.records) == 0


async def test_open_issue_success_with_labels_and_event(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    env = await setup_test_env(tmp_path, paths, databases, keystore, git_env, agent="evaluator")
    tool = OpenIssueTool(env.workspace, env.github, env.events, env.http, databases)

    # 1. Open first issue: labels created once
    res1 = await tool.invoke(
        env.ctx,
        {
            "title": "Model drift detected",
            "body": "Validation loss spiked by 25%",
            "category": "model",
            "priority": "high",
        },
    )
    assert res1.is_error is False
    assert "Issue #1 opened: https://github.com/test-owner/test-repo/issues/1" in res1.content

    assert len(env.events.records) == 1
    evt1 = env.events.records[0]
    assert evt1["type"] == "issue.opened"
    assert evt1["source"] == "evaluator"
    assert evt1["subject_id"] == "1"
    assert evt1["payload"] == {
        "number": 1,
        "title": "Model drift detected",
        "url": "https://github.com/test-owner/test-repo/issues/1",
        "state": "open",
        "category": "model",
        "priority": "high",
    }

    labels = fake_github.state.labels.get("test-owner/test-repo", set())
    assert {"agent:evaluator", "category:model", "priority:high"}.issubset(labels)

    # 2. Open second issue with same labels: ensure_labels does not fail on existing labels
    res2 = await tool.invoke(
        env.ctx,
        {
            "title": "Second drift issue",
            "body": "Another observation",
            "category": "model",
            "priority": "high",
        },
    )
    assert res2.is_error is False
    assert "Issue #2 opened: https://github.com/test-owner/test-repo/issues/2" in res2.content
    assert len(env.events.records) == 2


async def test_comment_on_issue_no_github_link(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    env = await setup_test_env(tmp_path, paths, databases, keystore, git_env, remote_url=None)
    tool = CommentOnIssueTool(env.workspace, env.github, env.events, env.http)
    res = await tool.invoke(env.ctx, {"number": 1, "body": "Hello"})
    assert res.is_error is True
    assert res.content == "Project has no GitHub link."


async def test_comment_on_issue_no_token(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    env = await setup_test_env(tmp_path, paths, databases, keystore, git_env, with_token=False)
    tool = CommentOnIssueTool(env.workspace, env.github, env.events, env.http)
    res = await tool.invoke(env.ctx, {"number": 1, "body": "Hello"})
    assert res.is_error is True
    assert res.content == "GitHub integration is not configured."


async def test_comment_on_issue_success_and_event(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    env = await setup_test_env(tmp_path, paths, databases, keystore, git_env, agent="diagnostician")

    await create_issue(
        env.http,
        "gho_test_token",
        "test-owner/test-repo",
        title="Initial issue",
        body="Initial body",
    )

    tool = CommentOnIssueTool(env.workspace, env.github, env.events, env.http)
    res = await tool.invoke(
        env.ctx, {"number": 1, "body": "Root cause identified in preprocessor."}
    )
    assert res.is_error is False
    assert (
        "Commented on issue #1: https://github.com/test-owner/test-repo/issues/1#issuecomment-1"
        in res.content
    )

    assert len(env.events.records) == 1
    evt = env.events.records[0]
    assert evt["type"] == "issue.commented"
    assert evt["source"] == "diagnostician"
    assert evt["subject_id"] == "1"
    assert evt["payload"] == {
        "number": 1,
        "url": "https://github.com/test-owner/test-repo/issues/1",
        "title": "Initial issue",
    }


async def test_default_registry_wiring(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    env = await setup_test_env(tmp_path, paths, databases, keystore, git_env)

    # Without issue tool deps
    reg_minimal = default_registry()
    names_min = set(reg_minimal.names())
    assert "SearchIssues" not in names_min
    assert "OpenIssue" not in names_min
    assert "CommentOnIssue" not in names_min

    # With issue tool deps
    reg_full = default_registry(
        workspace=env.workspace,
        github=env.github,
        events=env.events,
        http=env.http,
        databases=databases,
    )
    names_full = set(reg_full.names())
    assert "SearchIssues" in names_full
    assert "OpenIssue" in names_full
    assert "CommentOnIssue" in names_full


async def test_get_and_set_issue_min_priority(databases: Databases) -> None:
    project_id = "prj_testpriority"
    assert await get_issue_min_priority(databases, project_id) == "medium"

    await set_issue_min_priority(databases, project_id, "critical")
    assert await get_issue_min_priority(databases, project_id) == "critical"

    await set_issue_min_priority(databases, project_id, "low")
    assert await get_issue_min_priority(databases, project_id) == "low"
