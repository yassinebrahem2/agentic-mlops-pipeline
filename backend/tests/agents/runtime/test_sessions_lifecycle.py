from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx2
import pytest
from sqlalchemy import select

from mlops_agents.agents.runtime.contract import (
    MessageCreated,
    MessageUpdated,
    MessageView,
    TextDelta,
    TextPart,
    TurnEnded,
)
from mlops_agents.agents.runtime.executor import UNEXPECTED_FAILURE_TEXT, TurnExecutor
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import messages, turns
from tests.agents.runtime.session_env import build_session_env, collect_until_turn_ended
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


async def test_recovery(
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
    store = env.store
    sid = await store.session_id(project_id, "orchestrator")

    # Manually inject a running turn and a queued turn (as left by crashed process)
    user_msg_1 = await store.add_message(
        project_id,
        sid,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="running turn trigger")],
        status="complete",
    )
    trn_crashed = await store.add_turn(project_id, sid, user_msg_1.id, "running")
    msg_run_agent = await store.add_message(
        project_id,
        sid,
        turn_id=trn_crashed,
        author_kind="agent",
        author_name="orchestrator",
        parts=[],
        status="streaming",
    )

    user_msg_2 = await store.add_message(
        project_id,
        sid,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="queued turn trigger")],
        status="queued",
    )
    trn_queued = await store.add_turn(project_id, sid, user_msg_2.id, "queued")

    # First get() triggers recovery and starts the queued turn!
    view = await manager.get(project_id, "orchestrator")

    # trn_crashed must be marked interrupted
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(turns.c.status).where(turns.c.id == trn_crashed))
        ).scalar_one()
        assert row == "interrupted"
        agent_status = (
            await conn.execute(select(messages.c.status).where(messages.c.id == msg_run_agent.id))
        ).scalar_one()
        assert agent_status == "interrupted"

    # And the queued turn is running or completes
    sub = manager.subscribe(project_id, "orchestrator", after_seq=view.last_seq)
    events = await collect_until_turn_ended(sub, timeout=10.0)
    assert any(isinstance(e, TurnEnded) and e.data.turn_id == trn_queued for e in events)

    await manager.close()


async def test_executor_final_write_and_publish_are_one_step_under_the_session_lock(
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

    orig_update_message = SessionStore.update_message
    written = asyncio.Event()
    release = asyncio.Event()

    async def update_then_wait(self: SessionStore, *args: Any, **kwargs: Any) -> MessageView:
        result = await orig_update_message(self, *args, **kwargs)
        if "parts" in kwargs and kwargs.get("status") == "complete":
            written.set()
            await release.wait()
        return result

    monkeypatch.setattr(SessionStore, "update_message", update_then_wait)

    stream = manager.subscribe(project_id, "orchestrator", after_seq=None)
    collector = asyncio.create_task(collect_until_turn_ended(stream))
    await manager.send(project_id, "orchestrator", "Do turn")
    await written.wait()

    # The executor has stored the final message and not yet published it: get() must wait.
    get_task = asyncio.create_task(manager.get(project_id, "orchestrator"))
    done, _ = await asyncio.wait({get_task}, timeout=0.3)
    assert not done

    release.set()
    view = await get_task
    events = await collector

    agent_message = next(m for m in view.messages if m.author_kind == "agent")
    assert agent_message.status == "complete"
    published_again = [
        e
        for e in events
        if isinstance(e, MessageUpdated)
        and e.data.message.id == agent_message.id
        and e.data.message.status == "complete"
        and e.seq > view.last_seq
    ]
    assert published_again == []

    await manager.close()


async def test_stopped_turn_message_not_modified_when_later_turn_interrupted(
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
    settings_svc = env.settings
    await settings_svc.set_default_model("orchestrator", f"{prov_name}:fake-slow")

    sub = manager.subscribe(project_id, "orchestrator", after_seq=None)

    # 1. Start turn 1 and stop it
    await manager.send(project_id, "orchestrator", "Turn 1 slow")
    async for ev in sub:
        if isinstance(ev, TextDelta):
            break
    await manager.stop(project_id, "orchestrator")

    ended_turn_1: TurnEnded | None = None
    async for ev in sub:
        if isinstance(ev, TurnEnded):
            ended_turn_1 = ev
            break
    assert ended_turn_1 is not None
    assert ended_turn_1.data.status == "stopped"

    # Confirm turn 1's agent message is stopped in DB
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        turn_1_msg = (
            await conn.execute(
                select(messages.c.id, messages.c.status).where(
                    messages.c.turn_id == ended_turn_1.data.turn_id
                )
            )
        ).one()
        assert turn_1_msg[1] == "stopped"

    # 2. Start turn 2 and interrupt it via close()
    await manager.send(project_id, "orchestrator", "Turn 2 slow")
    async for ev in sub:
        if isinstance(ev, TextDelta):
            break

    await manager.close()

    # 3. Verify turn 1's agent message is STILL "stopped", not "interrupted"
    async with engine.connect() as conn:
        status_after = (
            await conn.execute(select(messages.c.status).where(messages.c.id == turn_1_msg[0]))
        ).scalar_one()
        assert status_after == "stopped"


async def test_error_escaping_the_executor_fails_the_turn_and_the_worker_goes_on(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    manager = env.manager
    project_id = env.project_id

    orig_run = TurnExecutor.run
    calls = 0

    async def run_failing_once(self: TurnExecutor, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            session_id, turn_id = args[1], args[3]
            await env.store.add_message(
                project_id,
                session_id,
                turn_id=turn_id,
                author_kind="agent",
                author_name="orchestrator",
                parts=[],
                status="streaming",
            )
            raise RuntimeError("boom with details")
        return await orig_run(self, *args, **kwargs)

    monkeypatch.setattr(TurnExecutor, "run", run_failing_once)

    stream = manager.subscribe(project_id, "orchestrator", after_seq=None)
    await manager.send(project_id, "orchestrator", "Turn 1")
    await manager.send(project_id, "orchestrator", "Turn 2")
    first_events = await collect_until_turn_ended(stream)
    first = first_events[-1]
    second = (await collect_until_turn_ended(stream))[-1]

    # The turn ends with the same visible state as a failure inside the executor.
    created = [e for e in first_events if isinstance(e, MessageCreated)]
    system_created = [e for e in created if e.data.message.author_kind == "system"]
    assert [m.data.message.parts for m in system_created] == [
        [TextPart(type="text", text=UNEXPECTED_FAILURE_TEXT)]
    ]
    agent_updates = [
        e
        for e in first_events
        if isinstance(e, MessageUpdated) and e.data.message.author_kind == "agent"
    ]
    assert [e.data.message.status for e in agent_updates] == ["complete"]
    stored = (await manager.get(project_id, "orchestrator")).messages
    assert {m.author_kind: m.status for m in stored if m.turn_id == first.data.turn_id} == {
        "agent": "complete",
        "system": "complete",
    }

    assert isinstance(first, TurnEnded)
    assert first.data.status == "failed"
    assert first.data.error == UNEXPECTED_FAILURE_TEXT
    assert isinstance(second, TurnEnded)
    assert second.data.status == "done"
    assert "boom with details" in caplog.text

    await manager.close()
