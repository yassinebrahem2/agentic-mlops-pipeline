"""Project secrets: credentials a project's proposals and tracking sources need.

The one writer of project secrets, and the module functions here are the only builders of
their refs. Each stored ref is recorded in app.db's secret_refs (never its value), so slot
state and project removal work without listing or reading the key store.
"""

from __future__ import annotations

import asyncio
import re

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.clock import now_iso
from mlops_agents.credentials.keystore import KeyStore
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import secret_refs

_PROJECT_REF = re.compile(r"^project/([^/]+)/.+$")


def project_prefix(project_id: str) -> str:
    return f"project/{project_id}/"


def proposal_prefix(project_id: str, proposal_id: str) -> str:
    return f"{project_prefix(project_id)}proposal/{proposal_id}/"


def proposal_slot_ref(project_id: str, proposal_id: str, slot: int) -> str:
    return f"{proposal_prefix(project_id, proposal_id)}{slot}"


def tracking_ref(project_id: str, source_id: str) -> str:
    return f"{project_prefix(project_id)}tracking/{source_id}"


def _project_of(ref: str) -> str:
    match = _PROJECT_REF.match(ref)
    if match is None:
        raise ValueError(f"{ref!r} is not a project secret ref (project/<id>/...)")
    return match.group(1)


class ProjectSecrets:
    """Stores first and records second, and deletes the secret before its row: a key store
    failure leaves the row, so a retry or a later sweep still finds the secret."""

    def __init__(self, keystore: KeyStore, databases: Databases) -> None:
        self._keystore = keystore
        self._dbs = databases

    async def put(self, ref: str, value: str) -> None:
        project = _project_of(ref)
        await asyncio.to_thread(self._keystore.set, ref, value)
        await self._record(ref, project)

    async def move(self, src_ref: str, dst_ref: str) -> None:
        project = _project_of(dst_ref)
        value = await asyncio.to_thread(self._keystore.get, src_ref)
        if value is None:
            raise LookupError(f"No secret is stored under {src_ref}.")
        await asyncio.to_thread(self._keystore.set, dst_ref, value)
        await self._record(dst_ref, project)
        await self.delete(src_ref)

    async def delete(self, ref: str) -> None:
        await asyncio.to_thread(self._keystore.delete, ref)
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            await conn.execute(delete(secret_refs).where(secret_refs.c.ref == ref))

    async def delete_prefix(self, prefix: str) -> None:
        """Deletes every recorded ref under `prefix`, one at a time, so a failure leaves the
        rest recorded."""
        engine = await self._dbs.app()
        async with engine.connect() as conn:
            refs = list(
                await conn.scalars(
                    select(secret_refs.c.ref).where(
                        secret_refs.c.ref.startswith(prefix, autoescape=True)
                    )
                )
            )
        for ref in refs:
            await self.delete(ref)

    async def is_set(self, ref: str) -> bool:
        engine = await self._dbs.app()
        async with engine.connect() as conn:
            found = await conn.scalar(select(secret_refs.c.ref).where(secret_refs.c.ref == ref))
        return found is not None

    async def _record(self, ref: str, project: str) -> None:
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            await conn.execute(
                sqlite_insert(secret_refs)
                .values(ref=ref, project=project, created=now_iso())
                .on_conflict_do_nothing(index_elements=[secret_refs.c.ref])
            )
