from __future__ import annotations

from pathlib import Path

import httpx2
import pytest

from mlops_agents.errors import DomainError
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import (
    build_session_env,
    collect_until_turn_ended,
    until_idle,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


async def test_clear_context_resets_summary_and_records_event(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    event_log = SqliteEventLog(databases)
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        event_log=event_log,
    )
    manager = env.manager
    project_id = env.project_id
    store = env.store

    # 1. Send first message and complete turn
    view = await manager.get(project_id, "orchestrator")
    sub = manager.subscribe(project_id, "orchestrator", after_seq=view.last_seq)
    msg1 = await manager.send(project_id, "orchestrator", "First turn")
    await collect_until_turn_ended(sub)
    await until_idle(manager, project_id, "orchestrator")

    history_before = await store.messages(project_id, view.id)
    assert len(history_before) >= 2
    last_msg_id = history_before[-1].id

    # Set some summary to make sure clear resets it
    await store.set_summary(
        project_id, view.id, summary="Existing summary", compacted_through=msg1.id
    )

    # 2. Call clear
    await manager.clear(project_id, "orchestrator", person="alice")

    # 3. Check summary and compacted_through in store
    summary, compacted_through = await store.summary(project_id, view.id)
    assert summary is None
    assert compacted_through == last_msg_id

    # 4. Stored messages remain unchanged
    history_after = await store.messages(project_id, view.id)
    assert [m.id for m in history_after] == [m.id for m in history_before]

    # 5. Events recorded
    events = await event_log.page(project_id, types=["session.cleared"])
    assert len(events) == 1
    assert events[0].type == "session.cleared"
    assert events[0].source == "user"
    assert events[0].subject_id == view.id
    assert events[0].payload == {
        "agent": "orchestrator",
        "person": "alice",
        "last_message_id": last_msg_id,
    }

    # 6. Next turn request holds no earlier messages or summary
    fake_openai.state.requests.clear()
    await manager.send(project_id, "orchestrator", "Fresh turn")
    await collect_until_turn_ended(sub)

    # Inspect requests sent to fake_openai for the second turn
    assert len(fake_openai.state.requests) > 0
    prompt_msgs = fake_openai.state.requests[-1]["messages"]
    # The system message should have no summary
    assert "Existing summary" not in prompt_msgs[0]["content"]
    # No earlier user message text
    contents = [m.get("content", "") for m in prompt_msgs]
    assert not any("First turn" in str(c) for c in contents)
    assert any("Fresh turn" in str(c) for c in contents)


async def test_compact_now_under_threshold_compacts_and_records_event(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    event_log = SqliteEventLog(databases)
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        event_log=event_log,
    )
    manager = env.manager
    project_id = env.project_id
    store = env.store

    view = await manager.get(project_id, "orchestrator")
    sub = manager.subscribe(project_id, "orchestrator", after_seq=view.last_seq)

    # Run 3 turns so we have > 2 finished turns
    await manager.send(project_id, "orchestrator", "Turn 1")
    await collect_until_turn_ended(sub)
    await manager.send(project_id, "orchestrator", "Turn 2")
    await collect_until_turn_ended(sub)
    await manager.send(project_id, "orchestrator", "Turn 3")
    await collect_until_turn_ended(sub)
    await until_idle(manager, project_id, "orchestrator")

    # Before compact, summary is None (under threshold)
    summary_before, _ = await store.summary(project_id, view.id)
    assert summary_before is None

    # Call compact
    await manager.compact(project_id, "orchestrator")

    summary_after, compacted_through = await store.summary(project_id, view.id)
    assert summary_after is not None
    assert compacted_through is not None

    events = await event_log.page(project_id, types=["session.compacted"])
    assert len(events) == 1
    assert events[0].type == "session.compacted"
    assert events[0].source == "user"
    assert events[0].subject_id == view.id
    assert events[0].payload == {"agent": "orchestrator"}

    # Next turn carries the summary
    fake_openai.state.requests.clear()
    await manager.send(project_id, "orchestrator", "Turn 4")
    await collect_until_turn_ended(sub)
    assert len(fake_openai.state.requests) > 0
    prompt_msgs = fake_openai.state.requests[-1]["messages"]
    system_text = prompt_msgs[0]["content"]
    assert "summary of the conversation" in system_text.lower() or summary_after in system_text


async def test_compact_and_clear_raise_409_when_busy(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
    )
    manager = env.manager
    project_id = env.project_id
    store = env.store
    view = await manager.get(project_id, "orchestrator")

    # Case 1: queued turn exists
    await store.add_turn(project_id, view.id, "msg_dummy", "queued")
    with pytest.raises(DomainError) as exc_info:
        await manager.compact(project_id, "orchestrator")
    assert exc_info.value.code == "session_busy"
    assert exc_info.value.status == 409

    with pytest.raises(DomainError) as exc_info:
        await manager.clear(project_id, "orchestrator")
    assert exc_info.value.code == "session_busy"
    assert exc_info.value.status == 409

    # Clean up queued turn
    # Case 2: waiting turn exists
    engine = await databases.project(project_id)
    from sqlalchemy import delete

    from mlops_agents.storage.tables import turns

    async with engine.begin() as conn:
        await conn.execute(delete(turns))

    await store.add_turn(project_id, view.id, "msg_dummy2", "waiting")
    with pytest.raises(DomainError) as exc_info:
        await manager.compact(project_id, "orchestrator")
    assert exc_info.value.code == "session_busy"
    assert exc_info.value.status == 409

    with pytest.raises(DomainError) as exc_info:
        await manager.clear(project_id, "orchestrator")
    assert exc_info.value.code == "session_busy"
    assert exc_info.value.status == 409


async def test_compact_and_clear_raise_404_for_unknown_agent(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
    )
    manager = env.manager
    project_id = env.project_id

    with pytest.raises(DomainError) as exc_info:
        await manager.compact(project_id, "unknown_agent")
    assert exc_info.value.code == "agent_not_found"
    assert exc_info.value.status == 404

    with pytest.raises(DomainError) as exc_info:
        await manager.clear(project_id, "unknown_agent")
    assert exc_info.value.code == "agent_not_found"
    assert exc_info.value.status == 404
