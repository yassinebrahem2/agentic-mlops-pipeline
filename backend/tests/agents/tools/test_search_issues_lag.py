from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest

from mlops_agents.agents.tools.issues import SearchIssuesTool
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github import api
from mlops_agents.integrations.github.issues import (
    create_issue,
    list_open_issues,
)
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.tools.test_issues import setup_test_env
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_github import FakeGitHubState, RunningFakeGitHub, start_fake_github


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


async def test_issue_in_list_not_in_search_results_is_found(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When /search/issues lags behind, SearchIssues still finds newly opened issues."""
    env = await setup_test_env(tmp_path, paths, databases, keystore, git_env)
    token = "gho_test_token"
    await create_issue(
        env.http,
        token,
        "test-owner/test-repo",
        title="Fix model performance drop",
        body="Observed accuracy decrease",
        labels=["priority:high"],
    )

    # Simulate search API indexing lag: /search/issues returns empty results
    orig_request = api.request

    async def lag_request(
        http: httpx2.AsyncClient,
        method: str,
        path: str,
        token: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        permission: str | None = None,
    ) -> httpx2.Response:
        if path == "/search/issues":
            return httpx2.Response(
                200, json={"total_count": 0, "incomplete_results": False, "items": []}
            )
        return await orig_request(
            http, method, path, token, params=params, json=json, permission=permission
        )

    monkeypatch.setattr(api, "request", lag_request)
    import mlops_agents.integrations.github.issues as gh_issues

    monkeypatch.setattr(gh_issues, "request", lag_request)

    tool = SearchIssuesTool(env.tasks, env.github, env.http)
    res = await tool.invoke(env.ctx, {"query": "model"})
    assert res.is_error is False
    assert "#1" in res.content
    assert "Fix model performance drop" in res.content


async def test_pull_request_in_issues_endpoint_is_skipped(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    """Pull requests in /repos/{repo}/issues are ignored by SearchIssues and list_open_issues."""
    env = await setup_test_env(tmp_path, paths, databases, keystore, git_env)
    token = "gho_test_token"

    # Create an issue
    await create_issue(
        env.http,
        token,
        "test-owner/test-repo",
        title="Fix data ingestion timeout",
        body="Timeout after 300s",
    )

    # Create a pull request in the same repo with overlapping words
    async with httpx2.AsyncClient(base_url=fake_github.base_url) as client:
        await client.post(
            "/repos/test-owner/test-repo/pulls",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "title": "Fix data ingestion timeout PR",
                "head": "feature",
                "base": "main",
                "body": "PR implementing data timeout fix",
            },
        )

    tool = SearchIssuesTool(env.tasks, env.github, env.http)
    res = await tool.invoke(env.ctx, {"query": "data ingestion"})
    assert res.is_error is False
    assert "#1" in res.content
    assert "Fix data ingestion timeout" in res.content
    assert "PR" not in res.content
    assert "#2" not in res.content

    # list_open_issues directly verified
    issues = await list_open_issues(env.http, token, "test-owner/test-repo")
    assert len(issues) == 1
    assert issues[0].number == 1
    assert issues[0].title == "Fix data ingestion timeout"


async def test_no_match_returns_no_open_issues_found(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    """When no open issues match the query, return 'No open issues found.'"""
    env = await setup_test_env(tmp_path, paths, databases, keystore, git_env)
    token = "gho_test_token"
    await create_issue(
        env.http,
        token,
        "test-owner/test-repo",
        title="Fix model performance drop",
        body="Observed accuracy decrease",
    )

    tool = SearchIssuesTool(env.tasks, env.github, env.http)
    res = await tool.invoke(env.ctx, {"query": "nonexistent_query_word"})
    assert res.is_error is False
    assert res.content == "No open issues found."


async def test_query_filters_case_insensitive_all_words_in_title_or_body(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    """Query matches issues containing every word in title or body, case-insensitively."""
    env = await setup_test_env(tmp_path, paths, databases, keystore, git_env)
    token = "gho_test_token"

    # Issue 1 has "model" in title and "accuracy" in body -> matches "model accuracy"
    await create_issue(
        env.http,
        token,
        "test-owner/test-repo",
        title="Fix MODEL evaluation",
        body="Observed ACCURACY decrease on test set",
    )
    # Issue 2 has only "model" -> should NOT match "model accuracy"
    await create_issue(
        env.http,
        token,
        "test-owner/test-repo",
        title="Train new MODEL checkpoint",
        body="Using Adam optimizer",
    )
    # Issue 3 has only "accuracy" -> should NOT match "model accuracy"
    await create_issue(
        env.http,
        token,
        "test-owner/test-repo",
        title="Improve ACCURACY reporting",
        body="Add confusion matrix plot",
    )

    tool = SearchIssuesTool(env.tasks, env.github, env.http)
    res = await tool.invoke(env.ctx, {"query": "model accuracy"})
    assert res.is_error is False
    assert "#1" in res.content
    assert "Fix MODEL evaluation" in res.content
    assert "#2" not in res.content
    assert "#3" not in res.content


async def test_list_open_issues_pagination_and_limit(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
) -> None:
    """list_open_issues respects the limit parameter across pages."""
    env = await setup_test_env(tmp_path, paths, databases, keystore, git_env)
    token = "gho_test_token"

    for i in range(1, 4):
        await create_issue(
            env.http,
            token,
            "test-owner/test-repo",
            title=f"Issue {i}",
            body=f"Description {i}",
        )

    issues = await list_open_issues(env.http, token, "test-owner/test-repo", limit=2)
    assert len(issues) == 2


async def test_list_open_issues_domain_error_on_server_failure(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    fake_github: RunningFakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_open_issues raises DomainError on GitHub error."""
    env = await setup_test_env(tmp_path, paths, databases, keystore, git_env)
    token = "gho_test_token"

    async def fail_request(*args: Any, **kwargs: Any) -> httpx2.Response:
        return httpx2.Response(502)

    import mlops_agents.integrations.github.issues as gh_issues

    monkeypatch.setattr(gh_issues, "request", fail_request)

    with pytest.raises(DomainError) as exc_info:
        await list_open_issues(env.http, token, "test-owner/test-repo")
    assert exc_info.value.code == "github_error"
