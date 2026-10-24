"""In-memory event hub for project events (SSE streaming, replay, resync)."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncGenerator
from typing import Any, Final

from sqlalchemy import func, select

from mlops_agents.agents.runtime.contract import Resync, ResyncData
from mlops_agents.events.contract import EventRecord
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import events

_RING_BUFFER_SIZE: Final[int] = 1000
_SLOW_SUBSCRIBER_THRESHOLD: Final[int] = 10000


def row_to_event(row: Any) -> EventRecord:
    payload = json.loads(row.payload) if isinstance(row.payload, str) else row.payload
    return EventRecord(
        id=row.id,
        seq=row.seq,
        ts=row.ts,
        source=row.source,
        type=row.type,
        subject_id=row.subject_id,
        payload=payload,
    )


class EventHub:
    """In-memory event hub for project events."""

    def __init__(self) -> None:
        self._buffers: dict[str, deque[EventRecord]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[EventRecord | Resync | None]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, project_id: str) -> asyncio.Lock:
        lock = self._locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[project_id] = lock
        return lock

    async def publish(self, project_id: str, event: EventRecord) -> None:
        lock = self._get_lock(project_id)
        async with lock:
            buffer = self._buffers.setdefault(project_id, deque(maxlen=_RING_BUFFER_SIZE))
            buffer.append(event)
            subs = self._subscribers.get(project_id, set())
            for q in list(subs):
                if q.qsize() >= _SLOW_SUBSCRIBER_THRESHOLD:
                    while not q.empty():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    q.put_nowait(Resync(seq=event.seq, type="resync", data=ResyncData()))
                else:
                    q.put_nowait(event)

    async def subscribe(
        self, databases: Databases, project_id: str, after_seq: int | None
    ) -> AsyncGenerator[EventRecord | Resync, None]:
        lock = self._get_lock(project_id)
        async with lock:
            queue: asyncio.Queue[EventRecord | Resync | None] = asyncio.Queue()
            self._subscribers.setdefault(project_id, set()).add(queue)
            buffer = self._buffers.get(project_id)
            buffer_copy = list(buffer) if buffer else []

        try:
            engine = await databases.project(project_id)
            if buffer_copy:
                max_seq = buffer_copy[-1].seq
            else:
                async with engine.connect() as conn:
                    res = (await conn.execute(select(func.max(events.c.seq)))).scalar()
                    max_seq = int(res) if res is not None else 0

            replay: list[EventRecord] = []
            needs_resync = False

            if after_seq is None:
                last_yielded = max_seq
            elif after_seq < 0 or after_seq > max_seq:
                needs_resync = True
                last_yielded = max_seq
            elif after_seq == max_seq:
                last_yielded = max_seq
            elif buffer_copy and after_seq >= buffer_copy[0].seq - 1:
                replay = [e for e in buffer_copy if e.seq > after_seq]
                last_yielded = replay[-1].seq if replay else after_seq
            else:
                async with engine.connect() as conn:
                    if after_seq > 0:
                        exists = (
                            await conn.execute(
                                select(events.c.seq).where(events.c.seq == after_seq)
                            )
                        ).scalar()
                        if exists is None:
                            needs_resync = True
                    if not needs_resync:
                        rows = (
                            await conn.execute(
                                select(events)
                                .where(events.c.seq > after_seq)
                                .order_by(events.c.seq.asc())
                            )
                        ).all()
                        replay = [row_to_event(r) for r in rows]
                        last_yielded = replay[-1].seq if replay else after_seq
                    else:
                        last_yielded = max_seq

            if needs_resync:
                yield Resync(seq=max_seq, type="resync", data=ResyncData())

            for ev in replay:
                yield ev

            while True:
                ev = await queue.get()
                if ev is None:
                    break
                if ev.seq > last_yielded:
                    yield ev
                    last_yielded = ev.seq
        finally:
            async with lock:
                subs = self._subscribers.get(project_id)
                if subs is not None:
                    subs.discard(queue)

    async def close_project(self, project_id: str) -> None:
        lock = self._get_lock(project_id)
        async with lock:
            subs = self._subscribers.pop(project_id, set())
            self._buffers.pop(project_id, None)
            for q in subs:
                q.put_nowait(None)
