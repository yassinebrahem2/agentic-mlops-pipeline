"""The cached secret values never miss a key: not after a key change during a read, nor after a
keystore delete that failed."""

import asyncio
import threading

import pytest
from sqlalchemy import insert

from mlops_agents.providers.keys import StoredKeys
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import providers
from tests.conftest import MemoryKeyStore


class HeldKeyStore(MemoryKeyStore):
    """Once `hold_next` is set, holds the next `get` (run in a worker thread) until released."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self.hold_next = False
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self._loop = loop

    def get(self, ref: str) -> str | None:
        if self.hold_next:
            self.hold_next = False
            self._loop.call_soon_threadsafe(self.entered.set)
            self.release.wait(timeout=10)
        return super().get(ref)


class FailingDeleteKeyStore(MemoryKeyStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_deletes = False
        self.reads = 0

    def get(self, ref: str) -> str | None:
        self.reads += 1
        return super().get(ref)

    def delete(self, ref: str) -> None:
        if self.fail_deletes:
            raise OSError("key store unavailable")
        super().delete(ref)


async def _insert_provider(databases: Databases, provider_id: str) -> None:
    engine = await databases.app()
    async with engine.begin() as conn:
        await conn.execute(
            insert(providers).values(
                id=provider_id,
                name=provider_id,
                kind="hosted",
                base_url=f"https://{provider_id}.example/v1",
                status="ok",
                status_detail=None,
                created_at="2026-10-25T00:00:00Z",
            )
        )


async def test_a_key_added_during_a_read_is_in_the_next_values(databases: Databases) -> None:
    keystore = HeldKeyStore(asyncio.get_running_loop())
    stored = StoredKeys(databases, keystore)
    await _insert_provider(databases, "prv_a")
    await stored.add("prv_a", "key-1")

    keystore.hold_next = True
    read = asyncio.create_task(stored.all_values())
    await asyncio.wait_for(keystore.entered.wait(), timeout=5)
    await stored.add("prv_a", "key-2")
    keystore.release.set()

    # The read in flight returns what it read, and is not cached over the change.
    assert await asyncio.wait_for(read, timeout=5) == ["key-1"]
    assert await stored.all_values() == ["key-1", "key-2"]


async def test_a_failed_keystore_delete_on_remove_still_clears_the_cache(
    databases: Databases,
) -> None:
    keystore = FailingDeleteKeyStore()
    stored = StoredKeys(databases, keystore)
    await _insert_provider(databases, "prv_a")
    await stored.add("prv_a", "key-1")
    await stored.add("prv_a", "key-2")
    assert await stored.all_values() == ["key-1", "key-2"]

    keystore.fail_deletes = True
    with pytest.raises(OSError, match="key store unavailable"):
        await stored.remove("prv_a", 1)

    keystore.reads = 0
    assert await stored.all_values() == ["key-2"]
    assert keystore.reads == 1


async def test_a_failed_keystore_delete_on_delete_provider_still_clears_the_cache(
    databases: Databases,
) -> None:
    keystore = FailingDeleteKeyStore()
    stored = StoredKeys(databases, keystore)
    await _insert_provider(databases, "prv_a")
    await _insert_provider(databases, "prv_b")
    await stored.add("prv_a", "key-a1")
    await stored.add("prv_b", "key-b1")
    assert await stored.all_values() == ["key-a1", "key-b1"]

    keystore.fail_deletes = True
    with pytest.raises(OSError, match="key store unavailable"):
        await stored.delete_provider("prv_a")

    keystore.reads = 0
    assert await stored.all_values() == ["key-b1"]
    assert keystore.reads == 1
