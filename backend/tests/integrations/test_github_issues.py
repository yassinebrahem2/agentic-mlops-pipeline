from __future__ import annotations

from collections.abc import Iterator

import httpx2
import pytest

from mlops_agents.errors import DomainError
from mlops_agents.integrations.github import api
from mlops_agents.integrations.github.issues import (
    IssueComment,
    IssueSummary,
    comment_issue,
    create_issue,
    ensure_labels,
    search_issues,
)
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


async def test_create_and_comment_issue(fake_github: RunningFakeGitHub) -> None:
    async with httpx2.AsyncClient() as http:
        issue = await create_issue(
            http,
            token="ghp_test_token",
            repo="octo/repo",
            title="Bug in training",
            body="Model loss is NaN",
            labels=["agent:lead", "category:bug"],
        )
        assert isinstance(issue, IssueSummary)
        assert issue.number == 1
        assert issue.title == "Bug in training"
        assert issue.body == "Model loss is NaN"
        assert issue.html_url == "https://github.com/octo/repo/issues/1"
        assert issue.state == "open"
        assert issue.labels == ["agent:lead", "category:bug"]

        comment = await comment_issue(
            http,
            token="ghp_test_token",
            repo="octo/repo",
            number=issue.number,
            body="Investigating learning rate.",
        )
        assert isinstance(comment, IssueComment)
        assert comment.id == 1
        assert comment.body == "Investigating learning rate."
        assert comment.html_url == "https://github.com/octo/repo/issues/1#issuecomment-1"


async def test_search_issues(fake_github: RunningFakeGitHub) -> None:
    async with httpx2.AsyncClient() as http:
        # Create two issues
        await create_issue(
            http,
            token="ghp_test_token",
            repo="octo/repo",
            title="First issue: dataset drift",
            body="Drift detected in production",
        )
        await create_issue(
            http,
            token="ghp_test_token",
            repo="octo/repo",
            title="Second issue: pipeline timeout",
            body="ETL job killed after 60s",
        )

        # Search specific
        results = await search_issues(http, token="ghp_test_token", repo="octo/repo", query="drift")
        assert len(results) == 1
        assert results[0].number == 1
        assert "drift" in results[0].title

        # Search all open issues with empty query
        all_open = await search_issues(http, token="ghp_test_token", repo="octo/repo", query="")
        assert len(all_open) == 2


async def test_ensure_labels(fake_github: RunningFakeGitHub) -> None:
    async with httpx2.AsyncClient() as http:
        # First call creates the labels
        await ensure_labels(
            http,
            token="ghp_test_token",
            repo="octo/repo",
            labels=["priority:high", "agent:researcher"],
        )
        assert "priority:high" in fake_github.state.labels["octo/repo"]
        assert "agent:researcher" in fake_github.state.labels["octo/repo"]

        # Second call does not fail even though labels exist
        await ensure_labels(
            http,
            token="ghp_test_token",
            repo="octo/repo",
            labels=["priority:high", "agent:researcher"],
        )


async def test_issues_missing_permission_403(fake_github: RunningFakeGitHub) -> None:
    fake_github.state.simulate_permission_error["issues: write"] = 403
    async with httpx2.AsyncClient() as http:
        with pytest.raises(DomainError) as err:
            await create_issue(
                http,
                token="ghp_test_token",
                repo="octo/repo",
                title="Issue",
                body="Body",
            )
        assert err.value.code == "github_permission_missing"
        assert err.value.status == 403
        assert (
            err.value.message
            == "GitHub rejected the request because the token lacks the 'issues: write' permission."
        )


async def test_issues_missing_permission_404(fake_github: RunningFakeGitHub) -> None:
    fake_github.state.simulate_permission_error["issues: write"] = 404
    async with httpx2.AsyncClient() as http:
        with pytest.raises(DomainError) as err:
            await comment_issue(
                http,
                token="ghp_test_token",
                repo="octo/repo",
                number=1,
                body="Comment",
            )
        assert err.value.code == "github_permission_missing"
        assert err.value.status == 403
        assert (
            err.value.message
            == "GitHub rejected the request because the token lacks the 'issues: write' permission."
        )


async def test_ensure_labels_missing_permission(fake_github: RunningFakeGitHub) -> None:
    fake_github.state.simulate_permission_error["issues: write"] = 403
    async with httpx2.AsyncClient() as http:
        with pytest.raises(DomainError) as err:
            await ensure_labels(
                http,
                token="ghp_test_token",
                repo="octo/repo",
                labels=["priority:high"],
            )
        assert err.value.code == "github_permission_missing"
        assert err.value.status == 403
        assert (
            err.value.message
            == "GitHub rejected the request because the token lacks the 'issues: write' permission."
        )


async def test_issues_token_rejected(fake_github: RunningFakeGitHub) -> None:
    fake_github.state.required_token = "valid_token"
    async with httpx2.AsyncClient() as http:
        with pytest.raises(DomainError) as err:
            await search_issues(http, token="invalid_token", repo="octo/repo")
        assert err.value.code == "github_token_rejected"
        assert err.value.status == 422
