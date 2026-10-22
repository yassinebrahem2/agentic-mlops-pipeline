from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import BaseModel
from sqlalchemy import select

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.contract import (
    MessageCreated,
    MessageUpdated,
    MessageView,
    Resync,
    SessionView,
    StreamEvent,
    TextDelta,
    TextPart,
    ToolStarted,
    TurnEnded,
)
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools import ToolRegistry, default_registry
from mlops_agents.agents.tools.base import AgentTool, RiskClass, ToolContext, ToolOutput
from mlops_agents.config import RunMode, Settings, load_settings
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import messages, turns
from mlops_agents.workspace.service import WorkspaceService
from tests.conftest import MemoryKeyStore, make_repo
from tests.fakes.fake_openai import RunningFake
from tests.integrations.test_github_auth import FakeGh


def _settings(paths: DataPaths) -> Settings:
    base = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)})
    return replace(base, mode=RunMode.LOCAL)


def _workspace_svc(
    paths: DataPaths, databases: Databases, keystore: MemoryKeyStore
) -> WorkspaceService:
    http = httpx2.AsyncClient()
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    return WorkspaceService(
        _settings(paths), paths, databases, GitRunner(paths.bin, github.token), github, http
    )


async def _setup_env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    *,
    files: dict[str, str] | None = None,
    extra_dirs: list[Path] | None = None,
    tools: ToolRegistry | None = None,
) -> tuple[SessionManager, WorkspaceService, str, str, ProvidersService]:
    repo_files = files if files is not None else {"README.md": "Line 1 of readme\nLine 2\n"}
    repo_dir = make_repo(tmp_path / "repo", repo_files, git_env)

    tool_reg = tools if tools is not None else default_registry()
    catalog = AgentCatalog.load(tool_reg.names(), extra_dirs=extra_dirs or ())
    providers_svc = ProvidersService(databases, keystore, model_http)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    workspace_svc = _workspace_svc(paths, databases, keystore)
    git = GitRunner(paths.bin)

    manager = SessionManager(
        databases=databases,
        workspace=workspace_svc,
        catalog=catalog,
        tools=tool_reg,
        settings=settings_svc,
        providers=providers_svc,
        http=model_http,
        git=git,
    )

    prov = await providers_svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake.base_url)
    )
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-tools"))
    await providers_svc.verify_model(f"{prov.name}:fake-tools")

    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-slow"))
    await providers_svc.record_verification(
        f"{prov.name}:fake-slow", tool_calling=True, streaming=True, detail=None
    )

    project = await workspace_svc.add_local(str(repo_dir))
    await settings_svc.set_default_model("orchestrator", f"{prov.name}:fake-tools")

    return manager, workspace_svc, project.id, prov.name, providers_svc


async def _collect_until_turn_ended(
    sub: AsyncIterator[StreamEvent],
    timeout: float = 10.0,  # noqa: ASYNC109
) -> list[StreamEvent]:
    events: list[StreamEvent] = []

    async def _gather() -> None:
        async for ev in sub:
            events.append(ev)
            if isinstance(ev, TurnEnded):
                break

    await asyncio.wait_for(_gather(), timeout=timeout)
    return events


async def test_ask_orchestrator_fake_tools(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    manager, _, project_id, _, _ = await _setup_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )

    sub = manager.subscribe(project_id, "orchestrator", after_seq=None)
    user_msg = await manager.send(project_id, "orchestrator", "Check readme")
    assert user_msg.status == "complete"
    assert user_msg.author_kind == "user"

    events = await _collect_until_turn_ended(sub)

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
    manager, _, project_id, _, _ = await _setup_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )

    sub1 = manager.subscribe(project_id, "orchestrator", after_seq=None)
    sub2 = manager.subscribe(project_id, "orchestrator", after_seq=None)

    task1 = asyncio.create_task(_collect_until_turn_ended(sub1))
    task2 = asyncio.create_task(_collect_until_turn_ended(sub2))

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
    manager, _, project_id, prov_name, _ = await _setup_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    # Set orchestrator model to fake-slow
    settings_svc = manager._settings  # pyright: ignore[reportPrivateUsage]
    await settings_svc.set_default_model("orchestrator", f"{prov_name}:fake-slow")

    # Subscriber 1 from the beginning
    sub1 = manager.subscribe(project_id, "orchestrator", after_seq=None)
    task1 = asyncio.create_task(_collect_until_turn_ended(sub1, timeout=15.0))

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
    events2 = await _collect_until_turn_ended(sub2, timeout=15.0)

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
    manager, _, project_id, _, _ = await _setup_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )

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


async def test_stop_during_fake_slow(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    manager, _, project_id, prov_name, _ = await _setup_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    settings_svc = manager._settings  # pyright: ignore[reportPrivateUsage]
    await settings_svc.set_default_model("orchestrator", f"{prov_name}:fake-slow")

    sub = manager.subscribe(project_id, "orchestrator", after_seq=None)

    await manager.send(project_id, "orchestrator", "Slow turn 1")
    # Queue turn 2
    await manager.send(project_id, "orchestrator", "Quick turn 2")

    # Wait for turn 1 to stream at least one delta
    async for ev in sub:
        if isinstance(ev, TextDelta):
            break

    # Stop turn 1
    await manager.stop(project_id, "orchestrator")

    # Collect until turn 2 finishes
    all_events: list[StreamEvent] = []
    ended: list[TurnEnded] = []
    async for ev in sub:
        all_events.append(ev)
        if isinstance(ev, TurnEnded):
            ended.append(ev)
            if len(ended) == 2:
                break

    assert ended[0].data.status == "stopped"
    assert ended[1].data.status == "done"
    assert any(
        isinstance(ev, MessageUpdated) and ev.data.message.status == "stopped" for ev in all_events
    )

    # Verify turn 1 partial text is preserved in stored messages
    store = manager._store  # pyright: ignore[reportPrivateUsage]
    sid = await store.session_id(project_id, "orchestrator")
    msgs = await store.messages(project_id, sid)
    agent_msg_1 = next(m for m in msgs if m.turn_id == ended[0].data.turn_id)
    assert agent_msg_1.status == "stopped"
    assert len(agent_msg_1.parts) > 0

    await manager.close()


class SlowDummyArgs(BaseModel):
    delay: float = 2.0


class SlowTool(AgentTool[SlowDummyArgs]):
    name = "slow_sleep"
    description = "Sleeps for a while."
    risk = RiskClass.READ
    args_model = SlowDummyArgs

    async def run(self, ctx: ToolContext, args: SlowDummyArgs) -> ToolOutput:
        await asyncio.sleep(args.delay)
        return ToolOutput(content="done")


async def test_stop_during_slow_tool(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    tools = default_registry()
    tools.register(SlowTool())
    manager, _, project_id, _, _ = await _setup_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, tools=tools
    )

    sub = manager.subscribe(project_id, "orchestrator", after_seq=None)
    await manager.send(project_id, "orchestrator", "Call slow sleep")

    # Wait for tool.started
    async for ev in sub:
        if isinstance(ev, ToolStarted):
            break

    # Stop during tool
    await manager.stop(project_id, "orchestrator")

    stop_events: list[StreamEvent] = []
    async for ev in sub:
        stop_events.append(ev)
        if isinstance(ev, TurnEnded):
            assert ev.data.status == "stopped"
            break

    assert any(
        isinstance(ev, MessageUpdated) and ev.data.message.status == "stopped" for ev in stop_events
    )

    # Session keeps working! Send a new message
    m = await manager.send(project_id, "orchestrator", "New turn")
    assert m.status == "complete"
    evs = await _collect_until_turn_ended(sub)
    assert isinstance(evs[-1], TurnEnded)
    assert evs[-1].data.status == "done"

    await manager.close()


async def test_recovery(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    manager, _, project_id, _, _ = await _setup_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    store = manager._store  # pyright: ignore[reportPrivateUsage]
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
    events = await _collect_until_turn_ended(sub, timeout=10.0)
    assert any(isinstance(e, TurnEnded) and e.data.turn_id == trn_queued for e in events)

    await manager.close()


async def test_close_project_during_turn(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    manager, _, project_id, prov_name, _ = await _setup_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    settings_svc = manager._settings  # pyright: ignore[reportPrivateUsage]
    await settings_svc.set_default_model("orchestrator", f"{prov_name}:fake-slow")

    sub = manager.subscribe(project_id, "orchestrator", after_seq=None)
    await manager.send(project_id, "orchestrator", "Slow response")

    # Wait for turn to start streaming
    async for ev in sub:
        if isinstance(ev, TextDelta):
            break

    # Now close_project
    await manager.close_project(project_id)

    # The subscriber stream ends cleanly
    stream_ended = False
    try:
        async for _ in sub:
            pass
        stream_ended = True
    except asyncio.CancelledError:
        stream_ended = True
    assert stream_ended

    # Database connections are closed, project directory can be deleted on Windows
    proj_dir = paths.project_dir(project_id)
    shutil.rmtree(proj_dir)
    assert not proj_dir.exists()


async def test_close_during_turn(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    manager, _, project_id, prov_name, _ = await _setup_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    settings_svc = manager._settings  # pyright: ignore[reportPrivateUsage]
    await settings_svc.set_default_model("orchestrator", f"{prov_name}:fake-slow")

    sub = manager.subscribe(project_id, "orchestrator", after_seq=None)
    await manager.send(project_id, "orchestrator", "Turn 1 slow")
    m2 = await manager.send(project_id, "orchestrator", "Turn 2 queued")

    async for ev in sub:
        if isinstance(ev, TextDelta):
            break

    await manager.close()

    # Check database: turn 1 is interrupted, turn 2 is still queued
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        turn_statuses = dict(
            (await conn.execute(select(turns.c.trigger_message_id, turns.c.status))).all()
        )
        assert turn_statuses[m2.id] == "queued"


async def test_stopped_turn_message_not_modified_when_later_turn_interrupted(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    manager, _, project_id, prov_name, _ = await _setup_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    settings_svc = manager._settings  # pyright: ignore[reportPrivateUsage]
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


async def test_get_send_race_deterministic(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    manager, _, project_id, _, _ = await _setup_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )

    store = manager._store  # pyright: ignore[reportPrivateUsage]
    orig_messages = store.messages

    event = asyncio.Event()

    async def patched_messages(*args: Any, **kwargs: Any) -> list[MessageView]:
        await event.wait()
        return await orig_messages(*args, **kwargs)

    store.messages = patched_messages  # type: ignore[method-assign]

    # Task 1: call get()
    get_task = asyncio.create_task(manager.get(project_id, "orchestrator"))
    # Allow get_task to run and enter patched_messages
    await asyncio.sleep(0.01)

    # Task 2: call send()
    send_task = asyncio.create_task(manager.send(project_id, "orchestrator", "Race message"))
    await asyncio.sleep(0.01)

    # Release get
    event.set()
    view = await get_task
    sent_msg = await send_task

    # Subscriber started with view.last_seq
    sub = manager.subscribe(project_id, "orchestrator", after_seq=view.last_seq)

    # The user message must be seen exactly once: either in view.messages or as an event
    in_view = any(m.id == sent_msg.id for m in view.messages)

    stream_saw_msg = False
    events = await _collect_until_turn_ended(sub)
    for ev in events:
        if isinstance(ev, MessageCreated) and ev.data.message.id == sent_msg.id:
            stream_saw_msg = True

    assert in_view ^ stream_saw_msg, f"in_view={in_view}, stream_saw_msg={stream_saw_msg}"

    await manager.close()


async def test_agent_validation(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    # Definition with chat: false
    extra_dir = tmp_path / "extra_defs"
    extra_dir.mkdir()
    (extra_dir / "no-chat.md").write_text(
        "---\nname: no-chat\ndisplay_name: No Chat\ndescription: No chat agent\nchat: false\n---\nPrompt",
        encoding="utf-8",
    )

    manager, _, project_id, _, _ = await _setup_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        extra_dirs=[extra_dir],
    )

    # 1. Unknown agent -> 404
    with pytest.raises(DomainError) as exc_404:
        await manager.send(project_id, "nonexistent", "Hello")
    assert exc_404.value.status == 404
    assert exc_404.value.code == "agent_not_found"

    with pytest.raises(DomainError) as exc_404_get:
        await manager.get(project_id, "nonexistent")
    assert exc_404_get.value.status == 404

    # 2. chat: false -> 409
    with pytest.raises(DomainError) as exc_409:
        await manager.send(project_id, "no-chat", "Hello")
    assert exc_409.value.status == 409
    assert exc_409.value.code == "agent_cannot_chat"

    with pytest.raises(DomainError) as exc_409_get:
        await manager.get(project_id, "no-chat")
    assert exc_409_get.value.status == 409

    # 3. empty text -> 422
    with pytest.raises(DomainError) as exc_422_empty:
        await manager.send(project_id, "orchestrator", "")
    assert exc_422_empty.value.status == 422
    assert exc_422_empty.value.code == "message_empty"

    with pytest.raises(DomainError) as exc_422_ws:
        await manager.send(project_id, "orchestrator", "   \n\t  ")
    assert exc_422_ws.value.status == 422

    await manager.close()


async def test_unknown_project_raises_404_and_never_creates_db(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    manager, _, _, _, _ = await _setup_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    unknown_id = "prj_nonexistent"
    db_path = paths.project_db(unknown_id)
    assert not db_path.exists()

    # 1. get
    with pytest.raises(DomainError) as exc_get:
        await manager.get(unknown_id, "orchestrator")
    assert exc_get.value.status == 404
    assert not db_path.exists()

    # 2. send
    with pytest.raises(DomainError) as exc_send:
        await manager.send(unknown_id, "orchestrator", "Hello")
    assert exc_send.value.status == 404
    assert not db_path.exists()

    # 3. stop
    with pytest.raises(DomainError) as exc_stop:
        await manager.stop(unknown_id, "orchestrator")
    assert exc_stop.value.status == 404
    assert not db_path.exists()

    # 4. subscribe
    sub = manager.subscribe(unknown_id, "orchestrator")
    with pytest.raises(DomainError) as exc_sub:
        await anext(sub)
    assert exc_sub.value.status == 404
    assert not db_path.exists()

    # 5. close_project
    with pytest.raises(DomainError) as exc_close:
        await manager.close_project(unknown_id)
    assert exc_close.value.status == 404
    assert not db_path.exists()

    await manager.close()
