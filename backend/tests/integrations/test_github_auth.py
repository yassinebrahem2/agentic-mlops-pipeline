import httpx2
import pytest

from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.auth import GITHUB_KEYSTORE_REF, GitHubAuth
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
