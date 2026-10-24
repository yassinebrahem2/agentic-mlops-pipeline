"""In-memory event hub for project events (SSE streaming, replay, resync)."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncGenerator
from typing import Any, Final

from sqlalchemy import func, select

from mlops_agents.agents.runtime.contract import Resync, ResyncData
from mlops_agents.agents.runtime.hub import SLOW_SUBSCRIBER_THRESHOLD
from mlops_agents.events.contract import EventRecord
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import events

_RING_BUFFER_SIZE: Final[int] = 1000
_REPLAY_PAGE_SIZE: Final[int] = 500


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
        self._last_seq: dict[str, int] = {}
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
            self._last_seq[project_id] = max(self._last_seq.get(project_id, 0), event.seq)
            buffer = self._buffers.setdefault(project_id, deque(maxlen=_RING_BUFFER_SIZE))
            buffer.append(event)
            subs = self._subscribers.get(project_id, set())
            for q in list(subs):
                if q.qsize() >= SLOW_SUBSCRIBER_THRESHOLD:
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
            baseline_seq = self._last_seq.get(project_id, 0)
            queue: asyncio.Queue[EventRecord | Resync | None] = asyncio.Queue()
            self._subscribers.setdefault(project_id, set()).add(queue)
            buffer = self._buffers.get(project_id)
            buffer_copy = list(buffer) if buffer else []

        try:
            engine = await databases.project(project_id)

            replay: list[EventRecord] = []
            needs_resync = False

            if after_seq is None:
                last_yielded = baseline_seq
            else:
                if baseline_seq == 0:
                    async with engine.connect() as conn:
                        res = (await conn.execute(select(func.max(events.c.seq)))).scalar()
                        if res is not None:
                            baseline_seq = int(res)

                if after_seq < 0 or after_seq > baseline_seq:
                    needs_resync = True
                    last_yielded = baseline_seq
                elif after_seq == baseline_seq:
                    last_yielded = baseline_seq
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
                            current_after = after_seq
                            last_yielded = after_seq
                            while True:
                                rows = (
                                    await conn.execute(
                                        select(events)
                                        .where(events.c.seq > current_after)
                                        .order_by(events.c.seq.asc())
                                        .limit(_REPLAY_PAGE_SIZE)
                                    )
                                ).all()
                                if not rows:
                                    break
                                for r in rows:
                                    ev = row_to_event(r)
                                    yield ev
                                    current_after = ev.seq
                                    last_yielded = ev.seq
                        else:
                            last_yielded = baseline_seq

            if needs_resync:
                yield Resync(seq=baseline_seq, type="resync", data=ResyncData())

            for ev in replay:
                yield ev

            while True:
                ev = await queue.get()
                if ev is None:
                    break
                if isinstance(ev, Resync) or ev.seq > last_yielded:
                    yield ev
                    if ev.seq > last_yielded:
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
            self._last_seq.pop(project_id, None)
            for q in subs:
                q.put_nowait(None)
            self._locks.pop(project_id, None)
