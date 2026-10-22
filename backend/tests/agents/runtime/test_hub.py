from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from mlops_agents.agents.runtime.contract import (
    MessageView,
    Resync,
    StreamEvent,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from mlops_agents.agents.runtime.hub import SessionHub

STREAM_EVENT_ADAPTER: TypeAdapter[StreamEvent] = TypeAdapter(StreamEvent)


def _load_fold_fixture() -> dict[str, Any]:
    path = Path(__file__).parents[4] / "shared" / "fixtures" / "session-fold.json"
    data: dict[str, Any] = json.loads(path.read_text("utf-8"))
    return data


async def test_hub_fold_fixture() -> None:
    doc = _load_fold_fixture()
    hub = SessionHub("ses_fold", initial_seq=100)

    events_list: list[Any] = doc["events"]
    for event_data in events_list:
        event = STREAM_EVENT_ADAPTER.validate_python(event_data)
        await hub.publish(event)
        if event.seq == 106:
            assert hub.live_message is not None
            assert (
                hub.live_message.model_dump(mode="json") == doc["after_event"]["106"]["messages"][1]
            )
        elif event.seq == 107:
            assert hub.live_message is not None
            assert hub.live_message.model_dump(mode="json") == doc["end"]["messages"][1]

    assert hub.last_seq == 108


async def test_hub_sink_methods() -> None:
    hub = SessionHub("ses_sink")
    start_seq = hub.last_seq

    # 1. message (user created) does not set live_message
    user_msg = MessageView(
        id="msg_u1",
        turn_id=None,
        author_kind="user",
        author_name="user",
        from_agent=None,
        parts=[TextPart(type="text", text="hello")],
        status="complete",
        created_at="2026-10-21T18:00:00Z",
    )
    await hub.message(user_msg, created=True)
    assert hub.live_message is None
    assert hub.last_seq > start_seq

    # 2. message (agent created, streaming)
    agent_msg = MessageView(
        id="msg_a1",
        turn_id="trn_1",
        author_kind="agent",
        author_name="orchestrator",
        from_agent=None,
        parts=[],
        status="streaming",
        created_at="2026-10-21T18:00:01Z",
    )
    await hub.message(agent_msg, created=True)
    assert hub.live_message is not None
    assert hub.live_message.id == "msg_a1"
    assert hub.live_message.parts == []

    # 3. tool_started
    call = ToolCallPart(type="tool_call", id="c1", name="Read", input={"file_path": "README.md"})
    await hub.tool_started("msg_a1", call)
    assert hub.live_message.parts == [call]

    # 4. tool_finished
    res = ToolResultPart(type="tool_result", tool_call_id="c1", content="Line 1", is_error=False)
    await hub.tool_finished("msg_a1", res)
    assert len(hub.live_message.parts) == 2
    assert hub.live_message.parts[1] == res

    # 5. text deltas
    await hub.text("msg_a1", "Part ")
    assert len(hub.live_message.parts) == 3
    assert hub.live_message.parts[2] == TextPart(type="text", text="Part ")

    await hub.text("msg_a1", "one.")
    assert len(hub.live_message.parts) == 3
    assert hub.live_message.parts[2] == TextPart(type="text", text="Part one.")

    # 6. message (agent updated, complete)
    updated_msg = MessageView(
        id="msg_a1",
        turn_id="trn_1",
        author_kind="agent",
        author_name="orchestrator",
        from_agent=None,
        parts=list(hub.live_message.parts),
        status="complete",
        created_at="2026-10-21T18:00:01Z",
    )
    await hub.message(updated_msg, created=False)
    assert hub.live_message.status == "complete"

    # 7. turn_ended
    await hub.turn_ended("trn_1", "done")
    assert hub.last_seq > start_seq


async def test_hub_seq_strictly_increasing() -> None:
    hub = SessionHub("ses_seq")
    seqs: list[int] = []
    for i in range(50):
        await hub.text("msg_a1", f"delta {i}")
        seqs.append(hub.last_seq)

    for i in range(1, len(seqs)):
        assert seqs[i] > seqs[i - 1], f"Expected {seqs[i]} > {seqs[i - 1]}"


async def test_hub_ring_buffer_and_subscribe() -> None:
    hub = SessionHub("ses_ring")

    seqs: list[int] = []
    for i in range(1, 21):
        await hub.text("msg_a1", f"chunk_{i}")
        seqs.append(hub.last_seq)

    # 1. after_seq in buffer range (e.g. seqs[14], expecting next 5 events)
    events: list[StreamEvent] = []
    async for event in hub.subscribe(after_seq=seqs[14]):
        events.append(event)
        if len(events) == 5:
            break
    assert [e.seq for e in events] == seqs[15:20]
    assert all(not isinstance(e, Resync) for e in events)

    # 2. after_seq=None -> yields resync first
    async with aclosing(hub.subscribe(after_seq=None)) as sub:
        first_event = await anext(sub)
        assert isinstance(first_event, Resync)
        assert first_event.seq == hub.last_seq

    # 3. after_seq > current_seq -> yields resync first
    async with aclosing(hub.subscribe(after_seq=hub.last_seq + 100000)) as sub_future:
        first_future = await anext(sub_future)
        assert isinstance(first_future, Resync)
        assert first_future.seq == hub.last_seq

    # 4. after_seq < oldest - 1:
    # Publish 5500 events to overflow the 5000 maxlen buffer
    old_seq = seqs[0]
    for i in range(5500):
        await hub.text("msg_a1", f"burst_{i}")

    async with aclosing(hub.subscribe(after_seq=old_seq)) as sub_old:
        first_old = await anext(sub_old)
        assert isinstance(first_old, Resync)
        assert first_old.seq == hub.last_seq


async def test_hub_two_subscribers_receive_identical_events() -> None:
    hub = SessionHub("ses_two")
    stream1 = hub.subscribe(after_seq=hub.last_seq)
    stream2 = hub.subscribe(after_seq=hub.last_seq)

    events1: list[StreamEvent] = []
    events2: list[StreamEvent] = []

    async def collect(sub: AsyncIterator[StreamEvent], target: list[StreamEvent]) -> None:
        async for event in sub:
            target.append(event)
            if len(target) == 5:
                break

    task1 = asyncio.create_task(collect(stream1, events1))
    task2 = asyncio.create_task(collect(stream2, events2))

    for i in range(5):
        await hub.text("msg_a1", f"hello_{i}")

    await asyncio.gather(task1, task2)

    assert len(events1) == 5
    assert len(events2) == 5
    assert [e.model_dump(mode="json") for e in events1] == [
        e.model_dump(mode="json") for e in events2
    ]


async def test_hub_slow_subscriber_receives_resync() -> None:
    hub = SessionHub("ses_slow")
    async with aclosing(hub.subscribe(after_seq=hub.last_seq)) as sub:
        # Publish 10,005 events rapidly while subscriber is not reading
        for i in range(10005):
            await hub.text("msg_a1", f"chunk_{i}")

        # The subscriber reads now; should see Resync because backlog exceeded 10,000
        first_event = await anext(sub)
        assert isinstance(first_event, Resync)
        assert first_event.seq == hub.last_seq


async def test_hub_close_terminates_stream() -> None:
    hub = SessionHub("ses_close")
    sub = hub.subscribe(after_seq=hub.last_seq)

    received: list[StreamEvent] = []

    async def reader() -> None:
        async for ev in sub:
            received.append(ev)

    task = asyncio.create_task(reader())
    await hub.text("msg_a1", "hi")
    await asyncio.sleep(0.01)
    hub.close()
    await asyncio.wait_for(task, timeout=1.0)
    assert len(received) == 1
