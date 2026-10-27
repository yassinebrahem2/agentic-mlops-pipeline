from __future__ import annotations

from collections.abc import Iterator

import httpx2
import pytest

from mlops_agents.errors import DomainError
from mlops_agents.integrations.github import api
from mlops_agents.integrations.github.pulls import (
    PullRequest,
    create_pull,
    get_pull,
    update_pull,
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


async def test_create_and_get_pull(fake_github: RunningFakeGitHub) -> None:
    async with httpx2.AsyncClient() as http:
        pr = await create_pull(
            http,
            token="ghp_test_token",
            repo="octo/repo",
            title="Add feature",
            head="feature-branch",
            base="main",
            body="Implements the feature",
        )
        assert isinstance(pr, PullRequest)
        assert pr.number == 1
        assert pr.title == "Add feature"
        assert pr.body == "Implements the feature"
        assert pr.html_url == "https://github.com/octo/repo/pull/1"
        assert pr.state == "open"
        assert pr.merged is False

        fetched = await get_pull(http, token="ghp_test_token", repo="octo/repo", number=1)
        assert fetched.number == pr.number
        assert fetched.title == pr.title
        assert fetched.state == "open"
        assert fetched.merged is False


async def test_update_pull(fake_github: RunningFakeGitHub) -> None:
    async with httpx2.AsyncClient() as http:
        pr = await create_pull(
            http,
            token="ghp_test_token",
            repo="octo/repo",
            title="Initial Title",
            head="feature-branch",
            base="main",
            body="Initial Body",
        )

        updated = await update_pull(
            http,
            token="ghp_test_token",
            repo="octo/repo",
            number=pr.number,
            title="Updated Title",
            body="Updated Body",
        )
        assert updated.number == pr.number
        assert updated.title == "Updated Title"
        assert updated.body == "Updated Body"


async def test_merged_and_closed_pull(fake_github: RunningFakeGitHub) -> None:
    async with httpx2.AsyncClient() as http:
        pr = await create_pull(
            http,
            token="ghp_test_token",
            repo="octo/repo",
            title="To merge",
            head="feature-branch",
            base="main",
        )

        # Merge on fake
        fake_github.state.merge_pull("octo/repo", pr.number)
        merged_pr = await get_pull(http, token="ghp_test_token", repo="octo/repo", number=pr.number)
        assert merged_pr.merged is True
        assert merged_pr.state == "closed"

        # Create another PR and close it
        pr2 = await create_pull(
            http,
            token="ghp_test_token",
            repo="octo/repo",
            title="To close",
            head="fix-branch",
            base="main",
        )
        fake_github.state.close_pull("octo/repo", pr2.number)
        closed_pr = await get_pull(
            http, token="ghp_test_token", repo="octo/repo", number=pr2.number
        )
        assert closed_pr.merged is False
        assert closed_pr.state == "closed"


async def test_pull_missing_permission_403(fake_github: RunningFakeGitHub) -> None:
    fake_github.state.simulate_permission_error["pull requests: write"] = 403
    async with httpx2.AsyncClient() as http:
        with pytest.raises(DomainError) as err:
            await create_pull(
                http,
                token="ghp_test_token",
                repo="octo/repo",
                title="PR",
                head="feat",
                base="main",
            )
        assert err.value.code == "github_permission_missing"
        assert err.value.status == 403
        assert (
            err.value.message
            == "GitHub rejected the request because the token lacks the 'pull requests: write' permission."
        )


async def test_get_pull_deleted_pr_is_not_found_not_a_missing_permission(
    fake_github: RunningFakeGitHub,
) -> None:
    async with httpx2.AsyncClient() as http:
        with pytest.raises(DomainError) as err:
            await get_pull(http, token="ghp_test_token", repo="octo/repo", number=999)
        assert err.value.code == "github_pr_not_found"
        assert err.value.status == 404
        assert "#999" in err.value.message


async def test_get_pull_missing_permission_403(fake_github: RunningFakeGitHub) -> None:
    fake_github.state.simulate_permission_error["pull requests: write"] = 403
    async with httpx2.AsyncClient() as http:
        with pytest.raises(DomainError) as err:
            await get_pull(http, token="ghp_test_token", repo="octo/repo", number=1)
        assert err.value.code == "github_permission_missing"
        assert err.value.status == 403
        assert (
            err.value.message
            == "GitHub rejected the request because the token lacks the 'pull requests: write' permission."
        )


async def test_pull_token_rejected(fake_github: RunningFakeGitHub) -> None:
    fake_github.state.required_token = "good_token"
    async with httpx2.AsyncClient() as http:
        with pytest.raises(DomainError) as err:
            await get_pull(http, token="bad_token", repo="octo/repo", number=1)
        assert err.value.code == "github_token_rejected"
        assert err.value.status == 422


async def test_pull_unreadable_response() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"broken": True})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    async with client:
        with pytest.raises(DomainError) as err:
            await get_pull(client, token="token", repo="octo/repo", number=1)
        assert err.value.code == "github_error"
        assert err.value.status == 502
