import httpx
import pytest

from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.api import get_login, get_repo, list_repos


def _client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Bearer good":
            return httpx.Response(401, json={"message": "Bad credentials"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "octo"})
        if request.url.path == "/user/repos":
            page = int(request.url.params["page"])
            items = [
                {
                    "full_name": f"octo/r{page}-{i}",
                    "private": True,
                    "default_branch": "main",
                    "description": None,
                }
                for i in range(100 if page == 1 else 3)
            ]
            return httpx.Response(200, json=items)
        if request.url.path == "/repos/octo/demo":
            return httpx.Response(
                200,
                json={
                    "full_name": "Octo/Demo",
                    "private": False,
                    "default_branch": "trunk",
                    "description": "d",
                },
            )
        if request.url.path == "/repos/octo/broken":
            return httpx.Response(200, json={"name": "no full_name"})
        return httpx.Response(404, json={"message": "Not Found"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_login() -> None:
    assert await get_login(_client(), "good") == "octo"


async def test_rejected_token() -> None:
    with pytest.raises(DomainError) as err:
        await get_login(_client(), "bad")
    assert err.value.code == "github_token_rejected"


async def test_repos_are_paginated() -> None:
    repos = await list_repos(_client(), "good")
    assert len(repos) == 103


async def test_get_repo_and_missing_repo() -> None:
    repo = await get_repo(_client(), "good", "octo/demo")
    assert repo.full_name == "Octo/Demo"
    assert repo.default_branch == "trunk"
    with pytest.raises(DomainError) as err:
        await get_repo(_client(), "good", "octo/nope")
    assert err.value.code == "github_repo_not_found"


async def test_unreadable_answer() -> None:
    with pytest.raises(DomainError) as err:
        await get_repo(_client(), "good", "octo/broken")
    assert err.value.code == "github_error"


def _answering(status: int, headers: dict[str, str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, json={"message": "limited"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_primary_rate_limit_says_when_to_retry() -> None:
    # 1793023500 is 2026-10-26 14:05:00 UTC.
    http = _answering(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1793023500"})
    with pytest.raises(DomainError) as err:
        await list_repos(http, "good")
    assert (err.value.code, err.value.status) == ("github_rate_limited", 429)
    assert err.value.message == "GitHub's rate limit was reached. Try again after 14:05 UTC."


async def test_secondary_rate_limit_uses_retry_after() -> None:
    with pytest.raises(DomainError) as err:
        await get_login(_answering(429, {"retry-after": "60"}), "good")
    assert err.value.code == "github_rate_limited"
    assert err.value.message == "GitHub's rate limit was reached. Try again after 60 seconds."


async def test_rate_limit_without_a_time() -> None:
    with pytest.raises(DomainError) as err:
        await get_repo(_answering(429, {}), "good", "octo/demo")
    assert err.value.message == "GitHub's rate limit was reached. Try again in a few minutes."


async def test_plain_forbidden_keeps_its_error() -> None:
    with pytest.raises(DomainError) as err:
        await get_login(_answering(403, {"x-ratelimit-remaining": "4999"}), "good")
    assert err.value.code == "github_error"
    assert err.value.message == "GitHub answered with HTTP 403."
