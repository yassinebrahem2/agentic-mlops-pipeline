from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import (
    MessageCreated,
    MessageUpdated,
    MessageView,
    Resync,
    SessionView,
    StreamEvent,
    TextDelta,
    TextPart,
    TurnEnded,
)
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import build_session_env, collect_until_turn_ended
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


async def test_ask_orchestrator_fake_tools(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    manager = env.manager
    project_id = env.project_id

    sub = manager.subscribe(project_id, "orchestrator", after_seq=None)
    user_msg = await manager.send(project_id, "orchestrator", "Check readme")
    assert user_msg.status == "complete"
    assert user_msg.author_kind == "user"

    events = await collect_until_turn_ended(sub)

    # First event is Resync because after_seq was None
    assert isinstance(events[0], Resync)
    live_events = events[1:]

    types = [e.type for e in live_events]
    assert "message.created" in types
    assert "tool.started" in types
    assert "tool.finished" in types
    assert "text.delta" in types
    assert "message.updated" in types
    assert types[-1] == "turn.ended"

    turn_ended = live_events[-1]
    assert isinstance(turn_ended, TurnEnded)
    assert turn_ended.data.status == "done"

    # Verify seq numbers strictly increasing
    seqs = [e.seq for e in live_events]
    for i in range(1, len(seqs)):
        assert seqs[i] > seqs[i - 1]

    await manager.close()


async def test_two_subscribers_receive_identical_events(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    manager = env.manager
    project_id = env.project_id

    sub1 = manager.subscribe(project_id, "orchestrator", after_seq=None)
    sub2 = manager.subscribe(project_id, "orchestrator", after_seq=None)

    task1 = asyncio.create_task(collect_until_turn_ended(sub1))
    task2 = asyncio.create_task(collect_until_turn_ended(sub2))

    await manager.send(project_id, "orchestrator", "Check readme")
    events1, events2 = await asyncio.gather(task1, task2)

    assert len(events1) > 0
    assert len(events1) == len(events2)
    assert [e.model_dump(mode="json") for e in events1] == [
        e.model_dump(mode="json") for e in events2
    ]

    await manager.close()


async def test_mid_turn_subscriber_after_get(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    manager = env.manager
    project_id = env.project_id
    prov_name = env.provider
    # Set orchestrator model to fake-slow
    settings_svc = env.settings
    await settings_svc.set_default_model("orchestrator", f"{prov_name}:fake-slow")

    # Subscriber 1 from the beginning
    sub1 = manager.subscribe(project_id, "orchestrator", after_seq=None)
    task1 = asyncio.create_task(collect_until_turn_ended(sub1, timeout=15.0))

    await manager.send(project_id, "orchestrator", "Slow response please")

    # Wait until turn is running and streaming deltas
    view: SessionView | None = None
    for _ in range(50):
        await asyncio.sleep(0.05)
        v = await manager.get(project_id, "orchestrator")
        if v.busy and len(v.messages) >= 2:
            agent_msg = v.messages[-1]
            if agent_msg.parts and isinstance(agent_msg.parts[-1], TextPart):
                view = v
                break

    assert view is not None
    assert view.busy is True
    initial_text = "".join(p.text for p in view.messages[-1].parts if isinstance(p, TextPart))

    # Now subscriber 2 joins mid-turn with view.last_seq
    sub2 = manager.subscribe(project_id, "orchestrator", after_seq=view.last_seq)
    events2 = await collect_until_turn_ended(sub2, timeout=15.0)

    delta_text = "".join(e.data.text for e in events2 if isinstance(e, TextDelta))
    combined_text = initial_text + delta_text

    events1 = await task1
    final_updated = [e for e in events1 if isinstance(e, MessageUpdated)]
    assert final_updated
    final_text = "".join(
        p.text for p in final_updated[-1].data.message.parts if isinstance(p, TextPart)
    )

    assert combined_text == final_text
    await manager.close()


async def test_three_messages_sent_at_once_queue_order(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    manager = env.manager
    project_id = env.project_id

    sub = manager.subscribe(project_id, "orchestrator", after_seq=None)

    m1 = await manager.send(project_id, "orchestrator", "Msg 1")
    m2 = await manager.send(project_id, "orchestrator", "Msg 2")
    m3 = await manager.send(project_id, "orchestrator", "Msg 3")

    assert m1.status == "complete"
    assert m2.status == "queued"
    assert m3.status == "queued"

    view = await manager.get(project_id, "orchestrator")
    assert view.busy is True

    # Collect events until 3 turn.ended events have arrived
    all_events: list[StreamEvent] = []
    turn_ends = 0
    async for ev in sub:
        all_events.append(ev)
        if isinstance(ev, TurnEnded):
            turn_ends += 1
            if turn_ends == 3:
                break

    assert turn_ends == 3
    # Check that m2 and m3 were updated to complete
    updated_ids = [
        ev.data.message.id
        for ev in all_events
        if isinstance(ev, MessageUpdated) and ev.data.message.status == "complete"
    ]
    assert m2.id in updated_ids
    assert m3.id in updated_ids

    # Eventually view is not busy
    view_end = await manager.get(project_id, "orchestrator")
    assert view_end.busy is False

    await manager.close()


async def test_get_send_race_deterministic(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    manager = env.manager
    project_id = env.project_id

    orig_messages = SessionStore.messages

    entered_messages = asyncio.Event()
    release_messages = asyncio.Event()

    async def patched_messages(self: SessionStore, *args: Any, **kwargs: Any) -> list[MessageView]:
        entered_messages.set()
        await release_messages.wait()
        return await orig_messages(self, *args, **kwargs)

    monkeypatch.setattr(SessionStore, "messages", patched_messages)

    # Task 1: call get()
    get_task = asyncio.create_task(manager.get(project_id, "orchestrator"))
    await entered_messages.wait()

    # Task 2: call send()
    send_task = asyncio.create_task(manager.send(project_id, "orchestrator", "Race message"))

    # Release get
    release_messages.set()
    view = await get_task
    sent_msg = await send_task

    # Subscriber started with view.last_seq
    sub = manager.subscribe(project_id, "orchestrator", after_seq=view.last_seq)

    # The user message must be seen exactly once: either in view.messages or as an event
    in_view = any(m.id == sent_msg.id for m in view.messages)

    stream_saw_msg = False
    events = await collect_until_turn_ended(sub)
    for ev in events:
        if isinstance(ev, MessageCreated) and ev.data.message.id == sent_msg.id:
            stream_saw_msg = True

    assert in_view ^ stream_saw_msg, f"in_view={in_view}, stream_saw_msg={stream_saw_msg}"

    await manager.close()
