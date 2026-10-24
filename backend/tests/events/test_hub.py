"""Tests for EventHub (pub/sub, replay, resync, close)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mlops_agents.agents.runtime.contract import Resync
from mlops_agents.events.contract import EventRecord
from mlops_agents.events.hub import EventHub
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.storage.databases import Databases


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
async def test_hub_replay_older_than_buffer(databases: Databases) -> None:
    project_id = "prj_overflow1"
    hub = EventHub()
    event_log = SqliteEventLog(databases, hub=hub)

    # Record 1005 events: buffer maxlen is 1000, so events 1..5 fall out of buffer
    for i in range(1, 1006):
        await event_log.record(project_id, source="system", type="turn.started", payload={"n": i})

    # Subscribe at after_seq=3 (older than buffer which starts at 6)
    sub = event_log.subscribe(project_id, after_seq=3)

    first_replayed = await anext(sub)
    assert isinstance(first_replayed, EventRecord)
    assert first_replayed.seq == 4

    second_replayed = await anext(sub)
    assert isinstance(second_replayed, EventRecord)
    assert second_replayed.seq == 5

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
