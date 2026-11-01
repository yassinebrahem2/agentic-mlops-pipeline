from __future__ import annotations

from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.runtime.model_caller import ModelKeysExhausted, TurnFailed
from mlops_agents.errors import DomainError
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.paths import DataPaths
from mlops_agents.providers.errors import ProviderCallError, ProviderErrorKind
from mlops_agents.providers.models import Provider
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


async def test_compact_now_no_op_on_short_session(
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

    # Only 1 turn: <= 2 turns so compaction is a no-op
    await manager.send(project_id, "orchestrator", "Turn 1")
    await collect_until_turn_ended(sub)
    await until_idle(manager, project_id, "orchestrator")

    compacted = await manager.compact(project_id, "orchestrator")
    assert compacted is False

    summary, compacted_through = await store.summary(project_id, view.id)
    assert summary is None
    assert compacted_through is None

    events = await event_log.page(project_id, types=["session.compacted"])
    assert len(events) == 0


async def test_compact_now_maps_model_errors(
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
    view = await manager.get(project_id, "orchestrator")
    sub = manager.subscribe(project_id, "orchestrator", after_seq=view.last_seq)

    # 3 turns to make sure compaction is attempted
    for i in range(1, 4):
        await manager.send(project_id, "orchestrator", f"Turn {i}")
        await collect_until_turn_ended(sub)
    await until_idle(manager, project_id, "orchestrator")

    # 1. ModelKeysExhausted maps to 409
    provider = Provider(
        id="prv_test",
        name="openai",
        kind="hosted",
        base_url="https://api.openai.com/v1",
        status="ok",
        status_detail=None,
        keys=[],
    )
    exhausted_err = ModelKeysExhausted(
        provider=provider,
        model="gpt-4o",
        keys=[],
        description="Every usable key is rate limited.",
    )

    async def fail_exhausted(*args: object, **kwargs: object) -> None:
        raise exhausted_err

    monkeypatch.setattr(manager._caller, "call", fail_exhausted)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(DomainError) as exc_info:
        await manager.compact(project_id, "orchestrator")
    assert exc_info.value.status == 409
    assert exc_info.value.code == "model_keys_exhausted"
    assert "openai: no usable key for gpt-4o. Every usable key is rate limited." in str(
        exc_info.value.message
    )

    # 2. TurnFailed maps to 503
    async def fail_turn(*args: object, **kwargs: object) -> None:
        raise TurnFailed("Summarization model failed.")

    monkeypatch.setattr(manager._caller, "call", fail_turn)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(DomainError) as exc_info:
        await manager.compact(project_id, "orchestrator")
    assert exc_info.value.status == 503
    assert exc_info.value.code == "turn_failed"
    assert "Summarization model failed." in str(exc_info.value.message)

    # 3. ProviderCallError maps to 503
    async def fail_provider(*args: object, **kwargs: object) -> None:
        raise ProviderCallError(
            kind=ProviderErrorKind.TRANSIENT,
            message="Provider service unavailable",
        )

    monkeypatch.setattr(manager._caller, "call", fail_provider)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(DomainError) as exc_info:
        await manager.compact(project_id, "orchestrator")
    assert exc_info.value.status == 503
    assert exc_info.value.code == "provider_error"
    assert "Provider service unavailable" in str(exc_info.value.message)
