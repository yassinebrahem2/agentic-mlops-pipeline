"""SQLite-backed EventLog implementation (I11)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Sequence
from typing import Any

from sqlalchemy import insert, select

from mlops_agents.agents.runtime.contract import Resync
from mlops_agents.clock import now_iso
from mlops_agents.events.contract import EventLog, EventRecord, EventType
from mlops_agents.events.hub import EventHub, row_to_event
from mlops_agents.ids import new_id
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import events


class SqliteEventLog(EventLog):
    """Event log backed by project.db and an in-memory EventHub."""

    def __init__(self, databases: Databases, hub: EventHub | None = None) -> None:
        self._databases = databases
        self._hub = hub if hub is not None else EventHub()
        self._record_locks: dict[str, asyncio.Lock] = {}

    def _get_record_lock(self, project_id: str) -> asyncio.Lock:
        lock = self._record_locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            self._record_locks[project_id] = lock
        return lock

    async def record(
        self,
        project_id: str,
        *,
        source: str,
        type: EventType,
        payload: dict[str, Any],
        subject_id: str | None = None,
    ) -> EventRecord:
        lock = self._get_record_lock(project_id)
        async with lock:
            evt_id = new_id("evt")
            ts = now_iso()
            payload_json = json.dumps(payload)

            engine = await self._databases.project(project_id)
            async with engine.begin() as conn:
                result = await conn.execute(
                    insert(events).values(
                        id=evt_id,
                        ts=ts,
                        source=source,
                        type=type,
                        subject_id=subject_id,
                        payload=payload_json,
                    )
                )
                pk = result.inserted_primary_key
                if pk is None or not pk:
                    raise RuntimeError("Failed to insert event: no primary key returned")
                seq = int(pk[0])

            record = EventRecord(
                id=evt_id,
                seq=seq,
                ts=ts,
                source=source,
                type=type,
                subject_id=subject_id,
                payload=payload,
            )
            await self._hub.publish(project_id, record)
            return record

    def subscribe(
        self, project_id: str, after_seq: int | None
    ) -> AsyncGenerator[EventRecord | Resync, None]:
        return self._hub.subscribe(self._databases, project_id, after_seq)

    async def history(
        self, project_id: str, *, types: Sequence[EventType], limit: int = 200
    ) -> list[EventRecord]:
        """Newest first."""
        if not types:
            return []
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(events)
                    .where(events.c.type.in_(types))
                    .order_by(events.c.seq.desc())
                    .limit(limit)
                )
            ).all()

        return [row_to_event(r) for r in rows]

    async def page(
        self,
        project_id: str,
        *,
        subject_id: str | None = None,
        types: Sequence[EventType] | None = None,
        source: str | None = None,
        before: int | None = None,
        limit: int = 50,
    ) -> list[EventRecord]:
        """Newest first, keyset on seq, before exclusive."""
        engine = await self._databases.project(project_id)
        stmt = select(events)
        if subject_id is not None:
            stmt = stmt.where(events.c.subject_id == subject_id)
        if types is not None:
            stmt = stmt.where(events.c.type.in_(types))
        if source is not None:
            stmt = stmt.where(events.c.source == source)
        if before is not None:
            stmt = stmt.where(events.c.seq < before)
        stmt = stmt.order_by(events.c.seq.desc()).limit(limit)
        async with engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [row_to_event(r) for r in rows]

    async def close_project(self, project_id: str) -> None:
        lock = self._get_record_lock(project_id)
        async with lock:
            await self._hub.close_project(project_id)
            self._record_locks.pop(project_id, None)
