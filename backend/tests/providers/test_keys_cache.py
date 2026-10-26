"""The cached secret values never miss a key, also after a key change during a read."""

import asyncio
import threading

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
