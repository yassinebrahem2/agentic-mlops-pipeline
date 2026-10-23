import asyncio

import pytest
from sqlalchemy import select

from mlops_agents.agents.runtime.contract import (
    Part,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import llm_calls, turns


async def test_session_id_is_stable_per_agent(databases: Databases) -> None:
    store = SessionStore(databases)
    s1 = await store.session_id("prj_test", "orchestrator")
    s2 = await store.session_id("prj_test", "orchestrator")
    s3 = await store.session_id("prj_test", "coder")

    assert s1 == s2
    assert s1 != s3
    assert s1.startswith("ses_")
    assert s3.startswith("ses_")


async def test_messages_round_trip_and_parts_order(databases: Databases) -> None:
    store = SessionStore(databases)
    session_id = await store.session_id("prj_test", "orchestrator")
    parts: list[Part] = [
        TextPart(type="text", text="Hello 🚀 世界"),
        ToolCallPart(type="tool_call", id="call_1", name="Read", input={"path": "main.py"}),
        ToolResultPart(
            type="tool_result", tool_call_id="call_1", content="print('hello')", is_error=False
        ),
    ]
    msg = await store.add_message(
        "prj_test",
        session_id,
        author_kind="agent",
        author_name="orchestrator",
        parts=parts,
        status="complete",
    )
    assert msg.id.startswith("msg_")
    assert msg.parts == parts

    messages = await store.messages("prj_test", session_id)
    assert len(messages) == 1
    assert messages[0].id == msg.id
    assert messages[0].parts == parts
    assert isinstance(messages[0].parts[0], TextPart)
    assert isinstance(messages[0].parts[1], ToolCallPart)
    assert isinstance(messages[0].parts[2], ToolResultPart)
    assert messages[0].parts[0].text == "Hello 🚀 世界"


async def test_update_message_changes_parts_and_status(databases: Databases) -> None:
    store = SessionStore(databases)
    session_id = await store.session_id("prj_test", "orchestrator")
    msg = await store.add_message(
        "prj_test",
        session_id,
        author_kind="agent",
        author_name="orchestrator",
        parts=[TextPart(type="text", text="Initial")],
        status="streaming",
    )

    updated_parts: list[Part] = [TextPart(type="text", text="Updated content")]
    updated = await store.update_message(
        "prj_test",
        msg.id,
        parts=updated_parts,
        status="complete",
    )
    assert updated.id == msg.id
    assert updated.status == "complete"
    assert updated.parts == updated_parts

    fetched = await store.messages("prj_test", session_id)
    assert len(fetched) == 1
    assert fetched[0].status == "complete"
    assert fetched[0].parts == updated_parts


async def test_messages_after(databases: Databases) -> None:
    store = SessionStore(databases)
    session_id = await store.session_id("prj_test", "orchestrator")
    m1 = await store.add_message(
        "prj_test",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="1")],
        status="complete",
    )
    m2 = await store.add_message(
        "prj_test",
        session_id,
        author_kind="agent",
        author_name="orchestrator",
        parts=[TextPart(type="text", text="2")],
        status="complete",
    )
    m3 = await store.add_message(
        "prj_test",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="3")],
        status="complete",
    )

    after_m1 = await store.messages("prj_test", session_id, after=m1.id)
    assert [m.id for m in after_m1] == [m2.id, m3.id]

    after_m2 = await store.messages("prj_test", session_id, after=m2.id)
    assert [m.id for m in after_m2] == [m3.id]

    after_m3 = await store.messages("prj_test", session_id, after=m3.id)
    assert after_m3 == []

    after_non_existent = await store.messages("prj_test", session_id, after="non_existent")
    assert after_non_existent == []

    other_session = await store.session_id("prj_test", "coder")
    assert await store.messages("prj_test", other_session, after=m1.id) == []


async def test_queued_turns_come_back_oldest_first(databases: Databases) -> None:
    store = SessionStore(databases)
    session_id = await store.session_id("prj_test", "orchestrator")
    t1 = await store.add_turn("prj_test", session_id, "msg_1", "queued")
    t2 = await store.add_turn("prj_test", session_id, "msg_2", "queued")

    next_turn = await store.next_queued_turn("prj_test", session_id)
    assert next_turn == (t1, "msg_1")

    await store.set_turn("prj_test", t1, "running")
    next_turn2 = await store.next_queued_turn("prj_test", session_id)
    assert next_turn2 == (t2, "msg_2")


async def test_recover_turns_running_into_interrupted(databases: Databases) -> None:
    store = SessionStore(databases)
    session_id = await store.session_id("prj_test", "orchestrator")

    # running turn with streaming message
    t_running = await store.add_turn("prj_test", session_id, "msg_1", "running")
    msg_running = await store.add_message(
        "prj_test",
        session_id,
        author_kind="agent",
        author_name="orchestrator",
        parts=[TextPart(type="text", text="In progress")],
        status="streaming",
        turn_id=t_running,
    )

    # a finished system message of the running turn is not an incomplete message
    msg_system = await store.add_message(
        "prj_test",
        session_id,
        author_kind="system",
        author_name="system",
        parts=[TextPart(type="text", text="Note")],
        status="complete",
        turn_id=t_running,
    )

    # queued turn
    t_queued = await store.add_turn("prj_test", session_id, "msg_2", "queued")

    # waiting turn
    t_waiting = await store.add_turn("prj_test", session_id, "msg_3", "waiting")

    # done turn
    t_done = await store.add_turn("prj_test", session_id, "msg_4", "done")

    await store.recover("prj_test")

    engine = await databases.project("prj_test")
    async with engine.connect() as conn:
        turn_rows = {row.id: row for row in (await conn.execute(select(turns))).mappings().all()}
    assert turn_rows[t_running]["status"] == "interrupted"
    assert turn_rows[t_running]["ended_at"] is not None
    assert turn_rows[t_queued]["status"] == "queued"
    assert turn_rows[t_waiting]["status"] == "waiting"
    assert turn_rows[t_done]["status"] == "done"

    messages = await store.messages("prj_test", session_id)
    recovered_msg = next(m for m in messages if m.id == msg_running.id)
    assert recovered_msg.status == "interrupted"
    assert next(m for m in messages if m.id == msg_system.id).status == "complete"


async def test_set_summary_and_summary_round_trip(databases: Databases) -> None:
    store = SessionStore(databases)
    session_id = await store.session_id("prj_test", "orchestrator")
    summary, compacted_through = await store.summary("prj_test", session_id)
    assert summary is None
    assert compacted_through is None

    await store.set_summary("prj_test", session_id, "Summary text", "msg_05")
    summary2, compacted_through2 = await store.summary("prj_test", session_id)
    assert summary2 == "Summary text"
    assert compacted_through2 == "msg_05"


async def test_add_llm_call(databases: Databases) -> None:
    store = SessionStore(databases)
    session_id = await store.session_id("prj_test", "orchestrator")
    turn_id = await store.add_turn("prj_test", session_id, "msg_1", "running")

    await store.add_llm_call(
        "prj_test",
        turn_id,
        agent="orchestrator",
        model_address="groq/llama-3.3-70b-versatile",
        prompt_tokens=150,
        completion_tokens=42,
        latency_ms=850,
    )

    engine = await databases.project("prj_test")
    async with engine.connect() as conn:
        rows = (
            (await conn.execute(select(llm_calls).where(llm_calls.c.turn_id == turn_id)))
            .mappings()
            .all()
        )
    assert len(rows) == 1
    assert rows[0]["id"].startswith("llm_")
    assert rows[0]["agent"] == "orchestrator"
    assert rows[0]["model_address"] == "groq/llama-3.3-70b-versatile"
    assert rows[0]["prompt_tokens"] == 150
    assert rows[0]["completion_tokens"] == 42
    assert rows[0]["latency_ms"] == 850
    assert rows[0]["created_at"] is not None


async def test_write_cancelled_by_its_caller_still_completes(databases: Databases) -> None:
    store = SessionStore(databases)
    session_id = await store.session_id("prj_test", "orchestrator")
    message = await store.add_message(
        "prj_test",
        session_id,
        author_kind="agent",
        author_name="orchestrator",
        parts=[],
        status="streaming",
    )

    write = asyncio.create_task(
        store.update_message(
            "prj_test", message.id, parts=[TextPart(type="text", text="kept")], status="stopped"
        )
    )
    await asyncio.sleep(0)
    write.cancel()
    with pytest.raises(asyncio.CancelledError):
        await write

    # Stop and shutdown cancel a turn anywhere; the interrupted write must not be lost or
    # leave the database locked for the writes that follow.
    await store.set_summary("prj_test", session_id, "later write", message.id)
    stored = next(m for m in await store.messages("prj_test", session_id) if m.id == message.id)
    assert stored.status == "stopped"
    assert stored.parts == [TextPart(type="text", text="kept")]
