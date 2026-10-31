"""Tests for EventHub (pub/sub, replay, resync, close)."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from mlops_agents.agents.runtime.contract import Resync, ResyncData
from mlops_agents.events.contract import EventRecord
from mlops_agents.events.hub import EventHub
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.storage.databases import Databases

pytestmark = pytest.mark.serial


@pytest.mark.asyncio
async def test_hub_replay_and_live_events(databases: Databases) -> None:
    project_id = "prj_hub1"
    hub = EventHub()
    event_log = SqliteEventLog(databases, hub=hub)

    # Record 3 events
    for i in range(1, 4):
        await event_log.record(project_id, source="user", type="turn.started", payload={"i": i})

    # Subscribe with after_seq=1
    sub = event_log.subscribe(project_id, after_seq=1)

    # First replayed events should be 2 and 3
    ev2 = await anext(sub)
    assert isinstance(ev2, EventRecord)
    assert ev2.seq == 2

    ev3 = await anext(sub)
    assert isinstance(ev3, EventRecord)
    assert ev3.seq == 3

    # Now publish live event 4
    ev4_task = asyncio.create_task(anext(sub))
    await asyncio.sleep(0.01)
    await event_log.record(project_id, source="user", type="turn.ended", payload={"i": 4})

    ev4 = await asyncio.wait_for(ev4_task, timeout=1.0)
    assert isinstance(ev4, EventRecord)
    assert ev4.seq == 4

    await sub.aclose()


@pytest.mark.asyncio
async def test_hub_replay_older_than_buffer_paged(databases: Databases) -> None:
    project_id = "prj_overflow1"
    hub = EventHub()
    event_log = SqliteEventLog(databases, hub=hub)

    # Record 1205 events: buffer maxlen is 1000, replay page size is 500
    for i in range(1, 1206):
        await event_log.record(project_id, source="system", type="turn.started", payload={"n": i})

    # Subscribe at after_seq=3 (older than buffer which starts at 206)
    sub = event_log.subscribe(project_id, after_seq=3)

    replayed: list[int] = []
    for _ in range(1202):  # events 4 through 1205
        ev = await anext(sub)
        assert isinstance(ev, EventRecord)
        replayed.append(ev.seq)

    assert replayed[0] == 4
    assert replayed[500] == 504
    assert replayed[-1] == 1205

    await sub.aclose()


@pytest.mark.asyncio
async def test_hub_live_subscriber_does_not_lose_event_during_subscribe(
    databases: Databases,
) -> None:
    project_id = "prj_window1"
    hub = EventHub()
    event_log = SqliteEventLog(databases, hub=hub)

    # Record initial event 1
    await event_log.record(project_id, source="user", type="turn.started", payload={"n": 1})

    # Hook EventHub to record an event inside the subscribe setup window
    recorded = False

    async def recording_hook(pid: str) -> None:
        nonlocal recorded
        if not recorded and pid == project_id:
            recorded = True
            await event_log.record(project_id, source="user", type="message.sent", payload={"n": 2})

    hub.setup_hook = recording_hook

    sub = event_log.subscribe(project_id, after_seq=None)
    ev = await asyncio.wait_for(anext(sub), timeout=2.0)
    assert isinstance(ev, EventRecord)
    assert ev.seq == 2
    assert recorded
    await sub.aclose()


@pytest.mark.asyncio
async def test_hub_unknown_position_yields_resync(databases: Databases) -> None:
    project_id = "prj_resync1"
    hub = EventHub()
    event_log = SqliteEventLog(databases, hub=hub)

    await event_log.record(project_id, source="user", type="turn.started", payload={"x": 1})
    await event_log.record(project_id, source="user", type="turn.ended", payload={"x": 2})

    # after_seq > max_seq yields Resync
    sub1 = event_log.subscribe(project_id, after_seq=999)
    resync1 = await anext(sub1)
    assert isinstance(resync1, Resync)
    assert resync1.seq == 2
    assert resync1.type == "resync"
    await sub1.aclose()

    # after_seq < 0 yields Resync
    sub2 = event_log.subscribe(project_id, after_seq=-5)
    resync2 = await anext(sub2)
    assert isinstance(resync2, Resync)
    assert resync2.seq == 2
    await sub2.aclose()


@pytest.mark.asyncio
async def test_hub_close_project_terminates_stream(databases: Databases) -> None:
    project_id = "prj_close1"
    hub = EventHub()
    event_log = SqliteEventLog(databases, hub=hub)

    sub = event_log.subscribe(project_id, after_seq=None)

    async def consume(iterator: Any) -> list[object]:
        items: list[object] = []
        async for item in iterator:
            items.append(item)
        return items

    consume_task = asyncio.create_task(consume(sub))
    await asyncio.sleep(0.02)

    await hub.close_project(project_id)
    result = await asyncio.wait_for(consume_task, timeout=1.0)
    assert result == []


@pytest.mark.asyncio
async def test_hub_resync_does_not_lower_last_yielded(databases: Databases) -> None:
    project_id = "prj_resync2"
    hub = EventHub()
    event_log = SqliteEventLog(databases, hub=hub)

    for i in range(1, 4):
        await event_log.record(project_id, source="user", type="turn.started", payload={"i": i})

    sub = event_log.subscribe(project_id, after_seq=0)
    ev1 = await asyncio.wait_for(anext(sub), timeout=2.0)
    ev2 = await asyncio.wait_for(anext(sub), timeout=2.0)
    ev3 = await asyncio.wait_for(anext(sub), timeout=2.0)
    assert ev1.seq == 1
    assert ev2.seq == 2
    assert ev3.seq == 3

    subs_attr = "_subscribers"
    subs_map: dict[str, set[asyncio.Queue[EventRecord | Resync | None]]] = cast(
        dict[str, set[asyncio.Queue[EventRecord | Resync | None]]], getattr(hub, subs_attr)
    )
    q: asyncio.Queue[EventRecord | Resync | None] = next(iter(subs_map[project_id]))
    await q.put(Resync(seq=1, type="resync", data=ResyncData()))

    old_ev = EventRecord(
        id="evt_old",
        seq=2,
        ts="2026-10-24T00:00:00Z",
        source="user",
        type="turn.started",
        payload={},
        subject_id=None,
    )
    new_ev = EventRecord(
        id="evt_new",
        seq=4,
        ts="2026-10-24T00:00:00Z",
        source="user",
        type="turn.started",
        payload={},
        subject_id=None,
    )
    await q.put(old_ev)
    await q.put(new_ev)

    resync = await asyncio.wait_for(anext(sub), timeout=2.0)
    assert isinstance(resync, Resync)
    assert resync.seq == 1

    next_ev = await asyncio.wait_for(anext(sub), timeout=2.0)
    assert isinstance(next_ev, EventRecord)
    assert next_ev.seq == 4

    await sub.aclose()


@pytest.mark.asyncio
async def test_hub_subscribe_live_does_not_acquire_project_database(
    databases: Databases, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = "prj_nodb"
    hub = EventHub()

    called = False
    original_project = databases.project

    async def fake_project(pid: str) -> AsyncEngine:
        nonlocal called
        called = True
        return await original_project(pid)

    monkeypatch.setattr(databases, "project", fake_project)

    ready = asyncio.Event()

    async def on_setup(pid: str) -> None:
        ready.set()

    hub.setup_hook = on_setup
    sub = hub.subscribe(databases, project_id, after_seq=None)
    anext_task = asyncio.create_task(anext(sub))
    await ready.wait()

    ev = EventRecord(
        id="evt_1",
        seq=1,
        ts="2026-10-24T00:00:00Z",
        source="user",
        type="turn.started",
        payload={},
        subject_id=None,
    )
    await hub.publish(project_id, ev)

    received = await asyncio.wait_for(anext_task, timeout=1.0)
    assert received == ev
    assert not called
    await sub.aclose()


@pytest.mark.asyncio
async def test_hub_replay_from_database_does_not_leak_connection_across_yield(
    databases: Databases,
) -> None:
    project_id = "prj_poolleak"
    event_log = SqliteEventLog(databases)

    for i in range(1, 4):
        await event_log.record(project_id, source="user", type="turn.started", payload={"i": i})

    engine = await databases.project(project_id)
    # A fresh hub instance has an empty in-memory buffer, forcing replay from SQLite
    hub = EventHub()

    sub = hub.subscribe(databases, project_id, after_seq=0)
    pool = cast(AsyncAdaptedQueuePool, engine.pool)
    ev = await anext(sub)
    assert isinstance(ev, EventRecord)
    assert ev.seq == 1
    # Check that while waiting between yields, no connection is checked out from the pool
    assert pool.checkedout() == 0

    ev2 = await anext(sub)
    assert isinstance(ev2, EventRecord)
    assert ev2.seq == 2
    assert pool.checkedout() == 0

    await sub.aclose()


@pytest.mark.asyncio
async def test_hub_close_project_terminates_stream_promptly_under_slow_db(
    databases: Databases, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = "prj_slowdb"
    hub = EventHub()

    async def slow_project(pid: str) -> AsyncEngine:
        await asyncio.sleep(2.0)
        return await databases.project(pid)

    monkeypatch.setattr(databases, "project", slow_project)

    ready = asyncio.Event()

    async def on_setup(pid: str) -> None:
        ready.set()

    hub.setup_hook = on_setup
    sub = hub.subscribe(databases, project_id, after_seq=None)

    async def consume(iterator: Any) -> list[object]:
        items: list[object] = []
        async for item in iterator:
            items.append(item)
        return items

    consume_task = asyncio.create_task(consume(sub))
    await ready.wait()

    await hub.close_project(project_id)
    result = await asyncio.wait_for(consume_task, timeout=1.0)
    assert result == []
