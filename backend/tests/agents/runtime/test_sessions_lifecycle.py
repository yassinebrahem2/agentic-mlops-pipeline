from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
from sqlalchemy import select

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.contract import (
    MessageView,
    TextDelta,
    TextPart,
    TurnEnded,
)
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools import default_registry
from mlops_agents.config import RunMode, Settings, load_settings
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
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> tuple[SessionManager, WorkspaceService, str, str, Path]:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "Hello\n"}, git_env)
    tools = default_registry()
    catalog = AgentCatalog.load(tools.names())
    providers_svc = ProvidersService(databases, keystore, model_http)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    workspace_svc = _workspace_svc(paths, databases, keystore)
    project = await workspace_svc.add_local(str(repo_dir))

    prov = await providers_svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake_openai.base_url)
    )
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-tools"))
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-slow"))
    await providers_svc.verify_model(f"{prov.name}:fake-tools")
    await providers_svc.record_verification(
        f"{prov.name}:fake-slow", tool_calling=True, streaming=True, detail=None
    )

    await settings_svc.set_default_model("orchestrator", f"{prov.name}:fake-tools")

    manager = SessionManager(
        databases=databases,
        workspace=workspace_svc,
        catalog=catalog,
        tools=tools,
        settings=settings_svc,
        providers=providers_svc,
        http=model_http,
        git=GitRunner(paths.bin),
    )
    return manager, workspace_svc, project.id, prov.name, repo_dir


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


async def _collect_until_turn_ended(
    sub: Any,
    timeout: float = 5.0,  # noqa: ASYNC109
) -> list[Any]:
    events: list[Any] = []

    async def collector() -> None:
        async for ev in sub:
            events.append(ev)
            if isinstance(ev, TurnEnded):
                break

    await asyncio.wait_for(collector(), timeout=timeout)
    return events


async def test_deterministic_race_executor_write_holds_lock(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    """Critical 3 test: executor write + publish happens under the session lock."""
    manager, _, project_id, prov_name, _ = await _setup_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    settings_svc = manager._settings  # pyright: ignore[reportPrivateUsage]
    await settings_svc.set_default_model("orchestrator", f"{prov_name}:fake-tools")

    store = manager._store  # pyright: ignore[reportPrivateUsage]
    orig_update_message = store.update_message

    executor_in_update = asyncio.Event()
    release_update = asyncio.Event()

    async def patched_update(*args: Any, **kwargs: Any) -> MessageView:
        if kwargs.get("status") == "complete":
            executor_in_update.set()
            await release_update.wait()
        return await orig_update_message(*args, **kwargs)

    store.update_message = patched_update  # type: ignore[method-assign]

    # Start turn
    send_task = asyncio.create_task(manager.send(project_id, "orchestrator", "Do turn"))
    await executor_in_update.wait()

    # Concurrent get() must wait for lock, cannot see half-updated message
    get_task = asyncio.create_task(manager.get(project_id, "orchestrator"))

    # Release executor update
    release_update.set()

    await send_task
    view = await get_task

    # Subscriber starting with view.last_seq
    sub = manager.subscribe(project_id, "orchestrator", after_seq=view.last_seq)
    await _collect_until_turn_ended(sub, timeout=3.0)

    # Any agent message in view is complete
    agent_views = [m for m in view.messages if m.author_kind == "agent"]
    if agent_views:
        assert agent_views[0].status == "complete"

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
