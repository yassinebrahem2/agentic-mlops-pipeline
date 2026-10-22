from __future__ import annotations

import asyncio
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
from sqlalchemy import select

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.contract import TextDelta, TextPart, TurnEnded
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools import ToolRegistry, default_registry
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

    # Close project
    await manager.close_project(project_id)

    # The subscriber stream ends cleanly without raising CancelledError
    stream_ended = False
    async for _ in sub:
        pass
    stream_ended = True
    assert stream_ended

    # Database connections closed, project dir can be removed on Windows
    proj_dir = paths.project_dir(project_id)
    shutil.rmtree(proj_dir)
    assert not proj_dir.exists()


async def test_close_during_turn_leaves_turn_and_message_interrupted(
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

    # Check database: turn 1 is interrupted, turn 1's agent message is interrupted, turn 2 is queued
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        turn_statuses = dict(
            (await conn.execute(select(turns.c.trigger_message_id, turns.c.status))).all()
        )
        assert turn_statuses[m2.id] == "queued"

        agent_msg_status = (
            await conn.execute(select(messages.c.status).where(messages.c.author_kind == "agent"))
        ).scalar_one()
        assert agent_msg_status == "interrupted"


async def test_stop_between_turns_then_close_queued_turn_stays_queued(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    """Critical 1 test: Stop called when no child is running must not swallow subsequent close()."""
    manager, _, project_id, prov_name, _ = await _setup_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    settings_svc = manager._settings  # pyright: ignore[reportPrivateUsage]
    await settings_svc.set_default_model("orchestrator", f"{prov_name}:fake-tools")

    # Send turn 1 and wait for it to complete
    sub = manager.subscribe(project_id, "orchestrator", after_seq=None)
    await manager.send(project_id, "orchestrator", "Turn 1 fast")
    await _collect_until_turn_ended(sub)

    # Queue turn 2
    store = manager._store  # pyright: ignore[reportPrivateUsage]
    session_id = await store.session_id(project_id, "orchestrator")
    m2 = await store.add_message(
        project_id,
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Turn 2 queued")],
        status="queued",
    )
    t2 = await store.add_turn(project_id, session_id, m2.id, "queued")

    # Call stop between turns (no child is executing)
    await manager.stop(project_id, "orchestrator")

    # Now close manager
    await manager.close()

    # The queued turn must stay queued, worker did not start it
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        t2_status = (
            await conn.execute(select(turns.c.status).where(turns.c.id == t2))
        ).scalar_one()
        assert t2_status == "queued"
