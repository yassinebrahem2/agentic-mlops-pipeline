import json
from collections.abc import AsyncIterator, Callable

import httpx2
import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from mlops_agents.errors import DomainError
from mlops_agents.paths import DataPaths
from mlops_agents.providers.failover import AllKeysExhausted, KeyFailover
from mlops_agents.providers.models import DEFAULT_COOLDOWN_MINUTES, ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from tests.conftest import MemoryKeyStore
from tests.providers.fake_clock import FakeClock, Script, statuses, unauthorized

GOOD = "gsk_good_key_123456"
Handler = Callable[[httpx2.Request], httpx2.Response]
MakeService = Callable[..., ProvidersService]


def _handler(request: httpx2.Request) -> httpx2.Response:
    if request.headers.get("authorization") != f"Bearer {GOOD}":
        return httpx2.Response(401, json={"error": {"message": "Invalid API key"}})
    data = [
        {"id": "m1", "context_window": 8192},
        {"id": "m2", "context_length": 4096},
        {"id": "m3"},
        {"id": "m4", "context_window": "big"},
        {"name": "no id"},
    ]
    return httpx2.Response(200, json={"object": "list", "data": data})


@pytest.fixture
async def make_service(
    databases: Databases, keystore: MemoryKeyStore
) -> AsyncIterator[MakeService]:
    clients: list[httpx2.AsyncClient] = []

    def make(handler: Handler = _handler) -> ProvidersService:
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        clients.append(client)
        return ProvidersService(databases, keystore, client, KeyFailover(FakeClock()))

    yield make
    for client in clients:
        await client.aclose()


HOSTED = ProviderCreate(name="groq", kind="hosted", base_url="https://api.groq.com/openai/v1/")


async def test_create_and_duplicate(make_service: MakeService) -> None:
    svc = make_service()
    provider = await svc.create_provider(HOSTED)
    assert provider.base_url == "https://api.groq.com/openai/v1"
    assert provider.id.startswith("prv_")
    with pytest.raises(DomainError) as err:
        await svc.create_provider(HOSTED)
    assert err.value.code == "provider_exists"


def test_base_url_with_credentials_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must not carry credentials"):
        ProviderCreate(name="x", kind="hosted", base_url="https://u:p@x.io/v1")


async def test_add_key_stores_only_in_keystore(
    make_service: MakeService, keystore: MemoryKeyStore, paths: DataPaths
) -> None:
    svc = make_service()
    await svc.create_provider(HOSTED)
    provider = await svc.add_key("groq", f"  {GOOD}  ")
    assert [k.hint for k in provider.keys] == ["••••3456"]
    assert list(keystore.data.values()) == [GOOD]
    files = (paths.app_db, paths.app_db.with_name("app.db-wal"))
    stored = b"".join(p.read_bytes() for p in files if p.exists())
    assert GOOD.encode() not in stored
    with pytest.raises(DomainError) as err:
        await svc.add_key("groq", GOOD)
    assert err.value.code == "key_exists"


async def test_rejected_key_is_not_stored(
    make_service: MakeService, keystore: MemoryKeyStore
) -> None:
    svc = make_service()
    await svc.create_provider(HOSTED)
    with pytest.raises(DomainError) as err:
        await svc.add_key("groq", "wrong")
    assert err.value.code == "key_rejected"
    assert keystore.data == {}


async def test_unreachable_provider_error_hides_the_key(make_service: MakeService) -> None:
    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"cannot connect while sending {GOOD}")

    svc = make_service(boom)
    await svc.create_provider(HOSTED)
    with pytest.raises(DomainError) as err:
        await svc.add_key("groq", GOOD)
    assert err.value.code == "provider_unreachable"
    assert GOOD not in err.value.message


async def test_remove_key_and_delete_provider_clean_keystore(
    make_service: MakeService, keystore: MemoryKeyStore
) -> None:
    svc = make_service()
    await svc.create_provider(HOSTED)
    await svc.add_key("groq", GOOD)
    provider = await svc.remove_key("groq", 1)
    assert provider.keys == []
    assert keystore.data == {}
    await svc.add_key("groq", GOOD)
    await svc.add_model(ModelCreate(provider="groq", wire_model_id="m1"))
    await svc.delete_provider("groq")
    assert keystore.data == {}
    assert await svc.list_models() == []


async def test_remote_models_and_context_window(make_service: MakeService) -> None:
    svc = make_service()
    await svc.create_provider(HOSTED)
    await svc.add_key("groq", GOOD)
    remote = await svc.list_remote_models("groq")
    assert [(m.id, m.context_window) for m in remote] == [
        ("m1", 8192),
        ("m2", 4096),
        ("m3", None),
        ("m4", None),
    ]
    entry = await svc.add_model(ModelCreate(provider="groq", wire_model_id="m1"))
    assert entry.address == "groq:m1"
    assert entry.context_window == 8192
    assert entry.availability == "untested"
    manual = await svc.add_model(
        ModelCreate(provider="groq", wire_model_id="m3", context_window=2048)
    )
    assert manual.context_window == 2048
    updated = await svc.set_context_window("groq:m3", 16000)
    assert updated.context_window == 16000
    with pytest.raises(DomainError) as err:
        await svc.add_model(ModelCreate(provider="groq", wire_model_id="m1"))
    assert err.value.code == "model_exists"


async def test_answer_that_is_not_a_model_list(make_service: MakeService) -> None:
    def not_a_list(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=["not", "a", "list"])

    svc = make_service(not_a_list)
    await svc.create_provider(
        ProviderCreate(name="local", kind="self_hosted", base_url="http://localhost:11434/v1")
    )
    with pytest.raises(DomainError) as err:
        await svc.list_remote_models("local")
    assert err.value.code == "provider_error"


async def test_hosted_provider_without_key_cannot_list(make_service: MakeService) -> None:
    svc = make_service()
    await svc.create_provider(HOSTED)
    with pytest.raises(DomainError) as err:
        await svc.list_remote_models("groq")
    assert err.value.code == "no_keys"


async def test_detect_and_import_env_keys(make_service: MakeService) -> None:
    svc = make_service()
    env = {
        "GROQ_API_KEY": GOOD,
        "GROQ_API_KEY_1": "gsk_second_key_0000",
        "GROQ_API_KEY_3": "gap",
    }
    found = await svc.detect_env_keys(env)
    assert [(d.env_var, d.provider_name, d.provider_exists) for d in found] == [
        ("GROQ_API_KEY", "groq", False),
        ("GROQ_API_KEY_1", "groq", False),
        ("GROQ_API_KEY_3", "groq", False),
    ]
    assert all(GOOD not in json.dumps(d.model_dump()) for d in found)
    provider = await svc.import_env_key("GROQ_API_KEY", env)
    assert provider.name == "groq"
    assert provider.base_url == "https://api.groq.com/openai/v1"
    assert len(provider.keys) == 1
    with pytest.raises(DomainError) as err:
        await svc.import_env_key("SOMETHING_ELSE", env)
    assert err.value.code == "unknown_env_key"


async def test_detect_env_keys_with_gaps(make_service: MakeService) -> None:
    svc = make_service()
    found = await svc.detect_env_keys({"GROQ_API_KEY_2": "val2"})
    assert [d.env_var for d in found] == ["GROQ_API_KEY_2"]

    env = {
        "GROQ_API_KEY_3": "val3",
        "GROQ_API_KEY": "val0",
        "GROQ_API_KEY_1": "val1",
    }
    found = await svc.detect_env_keys(env)
    assert [d.env_var for d in found] == [
        "GROQ_API_KEY",
        "GROQ_API_KEY_1",
        "GROQ_API_KEY_3",
    ]


async def test_add_key_cleans_up_keystore_if_db_fails(
    make_service: MakeService, keystore: MemoryKeyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = make_service()
    await svc.create_provider(HOSTED)

    class DbError(RuntimeError):
        pass

    def failing_begin(*args: object, **kwargs: object) -> object:
        raise DbError("database disk image is malformed")

    monkeypatch.setattr(AsyncEngine, "begin", failing_begin)

    with pytest.raises(DbError, match="database disk image is malformed"):
        await svc.add_key("groq", GOOD)

    assert keystore.data == {}


async def test_add_key_chains_keystore_delete_failure(
    make_service: MakeService, keystore: MemoryKeyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = make_service()
    await svc.create_provider(HOSTED)

    class DbError(RuntimeError):
        pass

    class KeystoreDeleteError(RuntimeError):
        pass

    def failing_begin(*args: object, **kwargs: object) -> object:
        raise DbError("database failure")

    def failing_delete(ref: str) -> None:
        raise KeystoreDeleteError("keystore locked")

    monkeypatch.setattr(AsyncEngine, "begin", failing_begin)
    monkeypatch.setattr(keystore, "delete", failing_delete)

    with pytest.raises(DbError) as err:
        await svc.add_key("groq", GOOD)

    assert err.value.__cause__ is None
    assert err.value.__notes__ == [
        "The new key could not be removed from the key store either: keystore locked"
    ]


async def test_secret_values_caching_and_invalidation(
    make_service: MakeService,
) -> None:
    def _mock_handler(_req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"object": "list", "data": []})

    svc = make_service(handler=_mock_handler)
    await svc.create_provider(HOSTED)

    # Initially empty
    assert await svc.secret_values() == []

    # Add a key
    await svc.add_key("groq", GOOD)
    assert await svc.secret_values() == [GOOD]

    # Verify cached: modifying _cached_secrets directly affects subsequent call
    owner_attr = "_stored_keys"
    owner = getattr(svc, owner_attr)
    cache_attr = "_cached_secrets"
    setattr(owner, cache_attr, ["fake_cached"])
    assert await svc.secret_values() == ["fake_cached"]

    # add_key invalidates cache
    await svc.add_key("groq", GOOD + "_2")
    assert getattr(owner, cache_attr) is None
    secrets = await svc.secret_values()
    assert set(secrets) == {GOOD, GOOD + "_2"}

    # remove_key invalidates cache
    await svc.remove_key("groq", 2)
    assert getattr(owner, cache_attr) is None
    assert await svc.secret_values() == [GOOD]

    # delete_provider invalidates cache
    await svc.delete_provider("groq")
    assert getattr(owner, cache_attr) is None
    assert await svc.secret_values() == []


async def test_a_key_change_clears_the_providers_failover_state(
    databases: Databases, keystore: MemoryKeyStore
) -> None:
    def any_key(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"object": "list", "data": []})

    failover = KeyFailover(FakeClock())
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(any_key)) as client:
        svc = ProvidersService(databases, keystore, client, failover)
        await svc.create_provider(HOSTED)
        await svc.add_key("groq", GOOD)

        async def exhaust() -> list[str]:
            provider = await svc.get_provider("groq")
            with pytest.raises(AllKeysExhausted):
                attempt = Script(*(unauthorized() for _ in provider.keys))
                await failover.call(provider, "m1", attempt, cooldown_minutes=15)
            return [state.status for state in failover.states(provider, "m1")]

        assert await exhaust() == ["rejected"]
        provider = await svc.add_key("groq", "gsk_second_key_654321")
        assert statuses(failover.states(provider, "m1")) == ["ok", "ok"]

        assert await exhaust() == ["rejected", "rejected"]
        provider = await svc.remove_key("groq", 1)
        assert statuses(failover.states(provider, "m1")) == ["ok"]

        assert await exhaust() == ["rejected"]
        await svc.delete_provider("groq")
        await svc.create_provider(HOSTED)
        await svc.add_key("groq", GOOD)
        assert statuses(failover.states(provider, "m1")) == ["ok"]


async def test_cooldown_is_stored_and_read_back(make_service: MakeService) -> None:
    svc = make_service()
    created = await svc.create_provider(HOSTED)
    assert created.cooldown_minutes == DEFAULT_COOLDOWN_MINUTES

    updated = await svc.set_cooldown("groq", 90)

    assert updated.cooldown_minutes == 90
    assert (await svc.get_provider_by_id(created.id)).cooldown_minutes == 90
    with pytest.raises(DomainError) as err:
        await svc.set_cooldown("missing", 30)
    assert err.value.status == 404
