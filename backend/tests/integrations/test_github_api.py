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
