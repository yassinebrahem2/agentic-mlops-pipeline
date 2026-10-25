from __future__ import annotations

import asyncio
import secrets

from sqlalchemy import delete, func, insert, select, update

from mlops_agents.clock import now_iso
from mlops_agents.credentials import KeyStore
from mlops_agents.credentials.scrub import mask_key
from mlops_agents.errors import DomainError
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import provider_keys, providers


class StoredKeys:
    """The one owner of provider key values: their `provider_keys` rows, their keystore entries
    and the cached list of every value the audit log scrubs. Every method that changes keys
    drops that cache itself, so no caller has to remember to."""

    def __init__(self, databases: Databases, keystore: KeyStore) -> None:
        self._dbs = databases
        self._keystore = keystore
        self._cached_secrets: list[str] | None = None

    async def values_for(self, provider_id: str) -> list[str]:
        """The provider's key values in position order."""
        return await self._read(provider_id)

    async def all_values(self) -> list[str]:
        """Every stored value, cached until a key changes. The caller gets its own copy."""
        if self._cached_secrets is None:
            self._cached_secrets = await self._read(None)
        return list(self._cached_secrets)

    def invalidate(self) -> None:
        self._cached_secrets = None

    async def add(self, provider_id: str, value: str) -> None:
        """Store `value` as the provider's next key and mark the provider healthy again. The
        keystore entry is written first and removed again if the database write fails for any
        reason, cancellation included."""
        ref = f"provider/{provider_id}/{secrets.token_hex(8)}"
        await asyncio.to_thread(self._keystore.set, ref, value)
        engine = await self._dbs.app()
        try:
            async with engine.begin() as conn:
                last = await conn.scalar(
                    select(func.max(provider_keys.c.position)).where(
                        provider_keys.c.provider_id == provider_id
                    )
                )
                await conn.execute(
                    insert(provider_keys).values(
                        provider_id=provider_id,
                        position=(last or 0) + 1,
                        keystore_ref=ref,
                        hint=mask_key(value),
                        created_at=now_iso(),
                    )
                )
                await conn.execute(
                    update(providers)
                    .where(providers.c.id == provider_id)
                    .values(status="ok", status_detail=None)
                )
        except BaseException as db_err:
            try:
                await asyncio.to_thread(self._keystore.delete, ref)
            except Exception as del_err:
                # The database error is still the one to report; the failed clean-up rides along.
                db_err.add_note(
                    f"The new key could not be removed from the key store either: {del_err}"
                )
            raise
        self.invalidate()

    async def remove(self, provider_id: str, position: int) -> None:
        """Delete the key's row, then its keystore entry."""
        engine = await self._dbs.app()
        where = (provider_keys.c.provider_id == provider_id) & (
            provider_keys.c.position == position
        )
        async with engine.begin() as conn:
            ref = await conn.scalar(select(provider_keys.c.keystore_ref).where(where))
            if ref is None:
                raise DomainError("key_not_found", "That key is not saved.", status=404)
            await conn.execute(delete(provider_keys).where(where))
        await asyncio.to_thread(self._keystore.delete, ref)
        self.invalidate()

    async def delete_provider(self, provider_id: str) -> None:
        """Delete the provider row (its key rows go with it), then every keystore entry it had.
        The refs are read in the same transaction, so a key added meanwhile is not orphaned."""
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            refs = list(
                await conn.scalars(
                    select(provider_keys.c.keystore_ref).where(
                        provider_keys.c.provider_id == provider_id
                    )
                )
            )
            await conn.execute(delete(providers).where(providers.c.id == provider_id))
        for ref in refs:
            await asyncio.to_thread(self._keystore.delete, ref)
        self.invalidate()

    async def _read(self, provider_id: str | None) -> list[str]:
        """The one reader of values, for one provider or for all: each ref is read once and
        empty values are dropped."""
        query = select(provider_keys.c.keystore_ref).order_by(
            provider_keys.c.provider_id, provider_keys.c.position
        )
        if provider_id is not None:
            query = query.where(provider_keys.c.provider_id == provider_id)
        engine = await self._dbs.app()
        async with engine.connect() as conn:
            refs = list(await conn.scalars(query))
        values = [await asyncio.to_thread(self._keystore.get, ref) for ref in refs]
        return [v for v in values if v]
