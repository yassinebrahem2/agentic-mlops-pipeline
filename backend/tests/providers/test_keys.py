from collections.abc import AsyncIterator

import httpx2
import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from mlops_agents.errors import DomainError
from mlops_agents.providers.keys import StoredKeys
from mlops_agents.providers.models import ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import provider_keys, providers
from tests.conftest import MemoryKeyStore

HOSTED = ProviderCreate(name="groq", kind="hosted", base_url="https://api.groq.com/openai/v1")


class CountingKeyStore(MemoryKeyStore):
    def __init__(self) -> None:
        super().__init__()
        self.reads: list[str] = []

    def get(self, ref: str) -> str | None:
        self.reads.append(ref)
        return super().get(ref)


@pytest.fixture
def counting_keystore() -> CountingKeyStore:
    return CountingKeyStore()


@pytest.fixture
def stored(databases: Databases, counting_keystore: CountingKeyStore) -> StoredKeys:
    return StoredKeys(databases, counting_keystore)


@pytest.fixture
async def service(
    databases: Databases, counting_keystore: CountingKeyStore
) -> AsyncIterator[ProvidersService]:
    def accept_all(_req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"object": "list", "data": []})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(accept_all)) as client:
        yield ProvidersService(databases, counting_keystore, client)


async def _insert_provider(databases: Databases, provider_id: str, name: str) -> None:
    engine = await databases.app()
    async with engine.begin() as conn:
        await conn.execute(
            insert(providers).values(
                id=provider_id,
                name=name,
                kind="hosted",
                base_url=f"https://{name}.example/v1",
                status="needs_attention",
                status_detail="rejected",
                created_at="2026-10-25T00:00:00Z",
            )
        )


async def _stored_refs(databases: Databases) -> list[str]:
    engine = await databases.app()
    async with engine.connect() as conn:
        return list(
            await conn.scalars(
                select(provider_keys.c.keystore_ref).order_by(
                    provider_keys.c.provider_id, provider_keys.c.position
                )
            )
        )


# The module on its own


async def test_values_follow_position_and_exclude_other_providers(
    databases: Databases, stored: StoredKeys
) -> None:
    await _insert_provider(databases, "prv_a", "a")
    await _insert_provider(databases, "prv_b", "b")
    await stored.add("prv_a", "key-a1")
    await stored.add("prv_a", "key-a2")
    await stored.add("prv_b", "key-b1")
    assert await stored.values_for("prv_a") == ["key-a1", "key-a2"]
    assert await stored.values_for("prv_b") == ["key-b1"]
    assert await stored.values_for("prv_none") == []


async def test_empty_values_are_dropped(
    databases: Databases, stored: StoredKeys, counting_keystore: CountingKeyStore
) -> None:
    await _insert_provider(databases, "prv_a", "a")
    await stored.add("prv_a", "key-1")
    await stored.add("prv_a", "key-2")
    first = (await _stored_refs(databases))[0]
    counting_keystore.data[first] = ""
    assert await stored.values_for("prv_a") == ["key-2"]
    stored.invalidate()
    assert await stored.all_values() == ["key-2"]


async def test_a_ref_missing_from_the_keystore_is_dropped(
    databases: Databases, stored: StoredKeys, counting_keystore: CountingKeyStore
) -> None:
    await _insert_provider(databases, "prv_a", "a")
    await stored.add("prv_a", "key-1")
    counting_keystore.data.clear()
    assert await stored.values_for("prv_a") == []
    assert await stored.all_values() == []


async def test_each_ref_is_read_once_per_call(
    databases: Databases, stored: StoredKeys, counting_keystore: CountingKeyStore
) -> None:
    await _insert_provider(databases, "prv_a", "a")
    await _insert_provider(databases, "prv_b", "b")
    await stored.add("prv_a", "key-a1")
    await stored.add("prv_a", "key-a2")
    await stored.add("prv_b", "key-b1")
    refs = await _stored_refs(databases)

    counting_keystore.reads.clear()
    await stored.values_for("prv_a")
    assert len(counting_keystore.reads) == 2
    assert len(set(counting_keystore.reads)) == 2

    counting_keystore.reads.clear()
    await stored.all_values()
    assert sorted(counting_keystore.reads) == sorted(refs)


async def test_all_values_are_cached_and_returned_as_a_copy(
    databases: Databases, stored: StoredKeys, counting_keystore: CountingKeyStore
) -> None:
    await _insert_provider(databases, "prv_a", "a")
    await stored.add("prv_a", "key-1")
    first = await stored.all_values()
    assert first == ["key-1"]
    first.append("tampered")
    counting_keystore.reads.clear()
    assert await stored.all_values() == ["key-1"]
    assert counting_keystore.reads == []


async def test_add_invalidates_the_cache_and_hides_nothing(
    databases: Databases, stored: StoredKeys
) -> None:
    await _insert_provider(databases, "prv_a", "a")
    assert await stored.all_values() == []
    await stored.add("prv_a", "key-1")
    assert await stored.all_values() == ["key-1"]
    await stored.add("prv_a", "key-2")
    assert sorted(await stored.all_values()) == ["key-1", "key-2"]


async def test_add_numbers_positions_and_clears_the_provider_problem(
    databases: Databases, stored: StoredKeys
) -> None:
    await _insert_provider(databases, "prv_a", "a")
    await stored.add("prv_a", "key-1")
    await stored.add("prv_a", "key-2")
    engine = await databases.app()
    async with engine.connect() as conn:
        positions = list(
            await conn.scalars(select(provider_keys.c.position).order_by(provider_keys.c.position))
        )
        row = (await conn.execute(select(providers))).mappings().one()
        hints = list(await conn.scalars(select(provider_keys.c.hint)))
    assert positions == [1, 2]
    assert (row["status"], row["status_detail"]) == ("ok", None)
    assert "key-1" not in hints


async def test_remove_invalidates_the_cache_and_deletes_the_entry(
    databases: Databases, stored: StoredKeys, counting_keystore: CountingKeyStore
) -> None:
    await _insert_provider(databases, "prv_a", "a")
    await stored.add("prv_a", "key-1")
    await stored.add("prv_a", "key-2")
    assert sorted(await stored.all_values()) == ["key-1", "key-2"]
    await stored.remove("prv_a", 1)
    assert await stored.all_values() == ["key-2"]
    assert list(counting_keystore.data.values()) == ["key-2"]


async def test_remove_of_an_unknown_position_changes_nothing(
    databases: Databases, stored: StoredKeys, counting_keystore: CountingKeyStore
) -> None:
    await _insert_provider(databases, "prv_a", "a")
    await stored.add("prv_a", "key-1")
    assert await stored.all_values() == ["key-1"]
    with pytest.raises(DomainError) as err:
        await stored.remove("prv_a", 9)
    assert (err.value.code, err.value.status) == ("key_not_found", 404)
    assert list(counting_keystore.data.values()) == ["key-1"]
    counting_keystore.reads.clear()
    assert await stored.all_values() == ["key-1"]
    assert counting_keystore.reads == []


async def test_delete_provider_removes_row_then_entries_and_invalidates(
    databases: Databases, stored: StoredKeys, counting_keystore: CountingKeyStore
) -> None:
    await _insert_provider(databases, "prv_a", "a")
    await _insert_provider(databases, "prv_b", "b")
    await stored.add("prv_a", "key-a1")
    await stored.add("prv_b", "key-b1")
    assert sorted(await stored.all_values()) == ["key-a1", "key-b1"]
    await stored.delete_provider("prv_a")
    assert await stored.all_values() == ["key-b1"]
    assert list(counting_keystore.data.values()) == ["key-b1"]
    engine = await databases.app()
    async with engine.connect() as conn:
        assert list(await conn.scalars(select(providers.c.id))) == ["prv_b"]


async def test_failed_add_leaves_no_trace_and_keeps_the_cache(
    databases: Databases,
    stored: StoredKeys,
    counting_keystore: CountingKeyStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _insert_provider(databases, "prv_a", "a")
    await stored.add("prv_a", "key-1")
    assert await stored.all_values() == ["key-1"]

    class DbError(RuntimeError):
        pass

    def failing_begin(*args: object, **kwargs: object) -> object:
        raise DbError("database disk image is malformed")

    monkeypatch.setattr(AsyncEngine, "begin", failing_begin)
    with pytest.raises(DbError):
        await stored.add("prv_a", "key-2")
    monkeypatch.undo()

    assert list(counting_keystore.data.values()) == ["key-1"]
    assert len(await _stored_refs(databases)) == 1
    counting_keystore.reads.clear()
    assert await stored.all_values() == ["key-1"]
    assert counting_keystore.reads == []


async def test_failed_cleanup_is_noted_on_the_original_error(
    databases: Databases,
    stored: StoredKeys,
    counting_keystore: CountingKeyStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _insert_provider(databases, "prv_a", "a")

    def failing_begin(*args: object, **kwargs: object) -> object:
        raise RuntimeError("database failure")

    def failing_delete(ref: str) -> None:
        raise OSError("keystore locked")

    monkeypatch.setattr(AsyncEngine, "begin", failing_begin)
    monkeypatch.setattr(counting_keystore, "delete", failing_delete)
    with pytest.raises(RuntimeError) as err:
        await stored.add("prv_a", "key-1")
    assert err.value.__notes__ == [
        "The new key could not be removed from the key store either: keystore locked"
    ]


# Through the service


async def test_service_methods_read_through_the_one_owner(
    service: ProvidersService, counting_keystore: CountingKeyStore
) -> None:
    provider = await service.create_provider(HOSTED)
    await service.add_key("groq", "key-1")
    await service.add_key("groq", "key-2")
    assert await service.keys_for(provider.id) == ["key-1", "key-2"]
    assert await service.secret_values() == ["key-1", "key-2"]

    counting_keystore.reads.clear()
    await service.keys_for(provider.id)
    assert len(counting_keystore.reads) == 2
    counting_keystore.reads.clear()
    await service.secret_values()  # cached by the call above
    assert counting_keystore.reads == []


async def test_service_secret_values_returns_a_copy(service: ProvidersService) -> None:
    await service.create_provider(HOSTED)
    await service.add_key("groq", "key-1")
    values = await service.secret_values()
    values.append("tampered")
    assert await service.secret_values() == ["key-1"]


async def test_service_key_changes_invalidate_the_secret_cache(service: ProvidersService) -> None:
    await service.create_provider(HOSTED)
    assert await service.secret_values() == []
    await service.add_key("groq", "key-1")
    assert await service.secret_values() == ["key-1"]
    await service.add_key("groq", "key-2")
    assert await service.secret_values() == ["key-1", "key-2"]
    await service.remove_key("groq", 1)
    assert await service.secret_values() == ["key-2"]
    await service.delete_provider("groq")
    assert await service.secret_values() == []


async def test_service_failed_add_changes_nothing(
    service: ProvidersService,
    counting_keystore: CountingKeyStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await service.create_provider(HOSTED)
    await service.add_key("groq", "key-1")
    assert await service.secret_values() == ["key-1"]

    class DbError(RuntimeError):
        pass

    def failing_begin(*args: object, **kwargs: object) -> object:
        raise DbError("disk full")

    monkeypatch.setattr(AsyncEngine, "begin", failing_begin)
    with pytest.raises(DbError):
        await service.add_key("groq", "key-2")
    monkeypatch.undo()

    assert list(counting_keystore.data.values()) == ["key-1"]
    assert await service.secret_values() == ["key-1"]
    provider = await service.get_provider("groq")
    assert [k.position for k in provider.keys] == [1]
