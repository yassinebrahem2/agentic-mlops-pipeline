import httpx2
import pytest

from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.auth import GITHUB_KEYSTORE_REF, GhCli, GitHubAuth
from mlops_agents.storage.databases import Databases
from tests.conftest import MemoryKeyStore


class FakeGh:
    def __init__(self, value: str | None) -> None:
        self.value = value

    @property
    def available(self) -> bool:
        return True

    async def token(self) -> str:
        if self.value is None:
            raise DomainError("gh_not_logged_in", "not logged in", status=409)
        return self.value


def _http() -> httpx2.AsyncClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("authorization") in (
            "Bearer gho_a",
            "Bearer gho_b",
            "Bearer ghp_pasted",
        ):
            return httpx2.Response(200, json={"login": "octo"})
        return httpx2.Response(401, json={"message": "Bad credentials"})

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


async def test_gh_mode_reads_the_token_every_time(
    databases: Databases, keystore: MemoryKeyStore
) -> None:
    gh = FakeGh("gho_a")
    auth = GitHubAuth(databases, keystore, _http(), gh)
    status = await auth.enable_gh()
    assert (status.mode, status.login) == ("gh", "octo")
    gh.value = "gho_b"  # the user logged in again with gh
    assert await auth.token() == "gho_b"
    assert keystore.data == {}


async def test_token_mode_stores_the_token(databases: Databases, keystore: MemoryKeyStore) -> None:
    auth = GitHubAuth(databases, keystore, _http(), FakeGh(None))
    status = await auth.set_token(" ghp_pasted ")
    assert status.mode == "token"
    assert keystore.data == {GITHUB_KEYSTORE_REF: "ghp_pasted"}
    assert await auth.token() == "ghp_pasted"
    await auth.disconnect()
    assert (await auth.status()).mode == "none"
    assert keystore.data == {}


async def test_rejected_token_is_not_stored(databases: Databases, keystore: MemoryKeyStore) -> None:
    auth = GitHubAuth(databases, keystore, _http(), FakeGh(None))
    with pytest.raises(DomainError):
        await auth.set_token("ghp_wrong")
    assert keystore.data == {}


async def test_require_token_when_not_connected(
    databases: Databases, keystore: MemoryKeyStore
) -> None:
    auth = GitHubAuth(databases, keystore, _http(), FakeGh(None))
    with pytest.raises(DomainError) as err:
        await auth.require_token()
    assert err.value.code == "github_not_connected"


async def test_list_repos_when_not_connected(
    databases: Databases, keystore: MemoryKeyStore
) -> None:
    auth = GitHubAuth(databases, keystore, _http(), FakeGh(None))
    with pytest.raises(DomainError) as err:
        await auth.list_repos()
    assert err.value.code == "github_not_connected"


async def test_list_repos_when_connected(databases: Databases, keystore: MemoryKeyStore) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("authorization") == "Bearer gho_a":
            if request.url.path == "/user":
                return httpx2.Response(200, json={"login": "octo"})
            if request.url.path == "/user/repos":
                return httpx2.Response(
                    200,
                    json=[
                        {
                            "full_name": "octo/demo",
                            "private": False,
                            "default_branch": "main",
                            "description": "Demo repo",
                        }
                    ],
                )
        return httpx2.Response(401, json={"message": "Bad credentials"})

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    auth = GitHubAuth(databases, keystore, http, FakeGh("gho_a"))
    await auth.enable_gh()
    repos = await auth.list_repos()
    assert len(repos) == 1
    assert repos[0].full_name == "octo/demo"
    assert repos[0].private is False
    assert repos[0].default_branch == "main"
    assert repos[0].description == "Demo repo"


async def test_gh_mode_with_gh_cli_caches_token_briefly(
    databases: Databases, keystore: MemoryKeyStore
) -> None:
    calls = 0

    class CountingGhCli(GhCli):
        async def token(self) -> str:
            nonlocal calls
            calls += 1
            return "gho_a"

    cli = CountingGhCli()
    auth = GitHubAuth(databases, keystore, _http(), cli)
    # Enable gh mode
    await auth.enable_gh()
    assert calls == 1

    # First auth.token() populates cache
    assert await auth.token() == "gho_a"
    assert calls == 2

    # Second call returns cached token without calling cli.token()
    assert await auth.token() == "gho_a"
    assert calls == 2
