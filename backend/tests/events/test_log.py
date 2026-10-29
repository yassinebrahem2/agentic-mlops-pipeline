"""Tests for SqliteEventLog (I11)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mlops_agents.events.contract import EventRecord
from mlops_agents.events.hub import EventHub
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.storage.databases import Databases


@pytest.mark.asyncio
async def test_event_log_record_and_history(databases: Databases) -> None:
    project_id = "prj_test1"
    event_log = SqliteEventLog(databases)

    # Record 5 events of various types
    e1 = await event_log.record(project_id, source="user", type="turn.started", payload={"turn": 1})
    await event_log.record(
        project_id, source="orchestrator", type="message.sent", payload={"msg": "hi"}
    )
    await event_log.record(
        project_id, source="orchestrator", type="turn.ended", payload={"turn": 1, "status": "done"}
    )
    await event_log.record(project_id, source="user", type="turn.started", payload={"turn": 2})
    e5 = await event_log.record(
        project_id, source="orchestrator", type="turn.ended", payload={"turn": 2, "status": "done"}
    )

    assert e1.seq == 1
    assert e1.id.startswith("evt_")
    assert e5.seq == 5

    # History: newest first, filtered by type
    history = await event_log.history(project_id, types=["turn.started", "turn.ended"], limit=3)
    assert len(history) == 3
    # Newest first: e5, e4, e3
    assert [e.seq for e in history] == [5, 4, 3]
    assert [e.type for e in history] == ["turn.ended", "turn.started", "turn.ended"]

    # History: empty types returns empty list
    empty_history = await event_log.history(project_id, types=[], limit=10)
    assert empty_history == []

    # History for nonexistent project returns empty list
    other_history = await event_log.history("prj_other1", types=["turn.started"], limit=10)
    assert other_history == []


@pytest.mark.asyncio
async def test_event_log_concurrent_records_publish_in_seq_order(
    databases: Databases, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = "prj_concurrent1"
    hub = EventHub()
    event_log = SqliteEventLog(databases, hub=hub)

    # Patch databases.project so each transaction yields with variable delay,
    # proving the per-project lock prevents out-of-order commits/publishes.
    real_project = databases.project
    delays = [0.02, 0.001, 0.015, 0.001, 0.025]
    call_idx = 0

    class SlowEngineProxy:
        def __init__(self, target: AsyncEngine) -> None:
            self._target = target

        def __getattr__(self, name: str) -> Any:
            return getattr(self._target, name)

        @contextlib.asynccontextmanager
        async def begin(self) -> AsyncGenerator[AsyncConnection, None]:
            nonlocal call_idx
            delay = delays[call_idx % len(delays)]
            call_idx += 1
            async with self._target.begin() as conn:
                yield conn
            await asyncio.sleep(delay)

    async def mock_project(pid: str) -> AsyncEngine:
        eng = await real_project(pid)
        return cast(AsyncEngine, SlowEngineProxy(eng))

    monkeypatch.setattr(databases, "project", mock_project)

    sub = event_log.subscribe(project_id, after_seq=None)
    received: list[int] = []

    async def consume() -> None:
        async for ev in sub:
            if isinstance(ev, EventRecord):
                received.append(ev.seq)
                if len(received) == 20:
                    break

    consume_task = asyncio.create_task(consume())
    await asyncio.sleep(0.02)

    async def record_one(i: int) -> None:
        await event_log.record(project_id, source="user", type="turn.started", payload={"i": i})

    await asyncio.gather(*(record_one(i) for i in range(20)))

    await asyncio.wait_for(consume_task, timeout=5.0)
    await sub.aclose()
    assert received == list(range(1, 21))


@pytest.mark.asyncio
async def test_event_log_close_project_cleans_locks(databases: Databases) -> None:
    project_id = "prj_cleanlocks"
    hub = EventHub()
    event_log = SqliteEventLog(databases, hub=hub)

    await event_log.record(project_id, source="user", type="turn.started", payload={})
    rec_attr = "_record_locks"
    hub_attr = "_locks"
    rec_locks = cast(dict[str, Any], getattr(event_log, rec_attr))
    hub_locks = cast(dict[str, Any], getattr(hub, hub_attr))
    assert project_id in rec_locks
    assert project_id in hub_locks

    await event_log.close_project(project_id)
    assert project_id not in rec_locks
    assert project_id not in hub_locks


@pytest.mark.asyncio
async def test_event_log_page_reads(databases: Databases) -> None:
    project_id = "prj_page1"
    event_log = SqliteEventLog(databases)

    # Record events with different subjects, types, and sources
    for i in range(1, 11):
        subj = "inc_1" if i % 2 == 0 else "inc_2"
        src = "agent_a" if i <= 5 else "agent_b"
        ev_type = "incident.opened" if i % 3 == 0 else "turn.started"
        await event_log.record(
            project_id,
            source=src,
            type=ev_type,
            subject_id=subj,
            payload={"i": i},
        )

    # 1. Filter by subject
    p_subj = await event_log.page(project_id, subject_id="inc_1")
    assert all(e.subject_id == "inc_1" for e in p_subj)
    assert len(p_subj) == 5

    # 2. Filter by type
    p_type = await event_log.page(project_id, types=["incident.opened"])
    assert all(e.type == "incident.opened" for e in p_type)
    assert len(p_type) == 3

    # 3. Filter by source
    p_src = await event_log.page(project_id, source="agent_a")
    assert all(e.source == "agent_a" for e in p_src)
    assert len(p_src) == 5

    # 4. Keyset paging: newest first, before exclusive
    page1 = await event_log.page(project_id, limit=4)
    assert [e.seq for e in page1] == [10, 9, 8, 7]

    # An event arrives between pages
    await event_log.record(
        project_id, source="agent_b", type="turn.started", subject_id="inc_1", payload={"i": 11}
    )

    # Request page 2 with before=7 (the seq of the last item on page1)
    page2 = await event_log.page(project_id, before=page1[-1].seq, limit=4)
    assert [e.seq for e in page2] == [6, 5, 4, 3]

    page3 = await event_log.page(project_id, before=page2[-1].seq, limit=4)
    assert [e.seq for e in page3] == [2, 1]

    # No duplicates across pages
    all_seqs = [e.seq for e in page1] + [e.seq for e in page2] + [e.seq for e in page3]
    assert all_seqs == list(range(10, 0, -1))
