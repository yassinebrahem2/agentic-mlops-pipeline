from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx2
from sqlalchemy import update

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.agent import AgentBuilder
from mlops_agents.agents.runtime.contract import TextPart
from mlops_agents.agents.runtime.executor import TurnExecutor
from mlops_agents.agents.runtime.hub import AsyncRLock
from mlops_agents.agents.runtime.model_caller import ModelCaller
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools import default_registry
from mlops_agents.config import RunMode, Settings, load_settings
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.failover import KeyFailover
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import projects
from mlops_agents.workspace.service import WorkspaceService
from tests.agents.runtime.graph_env import RecordingSink, stage1_hooks
from tests.conftest import (
    STAGE_2_TOOLS,
    STAGE_3_HOOKS,
    MemoryKeyStore,
    make_repo,
    memory_project_secrets,
)
from tests.fakes.fake_openai import RunningFake
from tests.fakes.workspaces import FakeWorkspaces
from tests.integrations.test_github_auth import FakeGh
from tests.providers.fake_clock import FakeClock


async def _set_project_status(
    databases: Databases,
    project_id: str,
    *,
    status: str,
    detail: str | None = None,
) -> None:
    engine = await databases.app()
    async with engine.begin() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == project_id)
            .values(status=status, status_detail=detail)
        )


def _settings(paths: DataPaths) -> Settings:
    base = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)})
    return replace(base, mode=RunMode.LOCAL)


def _workspace_svc(
    paths: DataPaths, databases: Databases, keystore: MemoryKeyStore
) -> WorkspaceService:
    http = httpx2.AsyncClient()
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    return WorkspaceService(
        _settings(paths),
        paths,
        databases,
        GitRunner(paths.bin, github.token),
        github,
        http,
        memory_project_secrets(databases),
    )


async def _setup_executor_env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    *,
    files: dict[str, str] | None = None,
    extra_dirs: list[Path] | None = None,
) -> tuple[
    TurnExecutor,
    SessionStore,
    AgentSettingsService,
    WorkspaceService,
    str,
    str,
    str,
    str,
    str,
]:
    repo_files = files if files is not None else {"README.md": "Hello world\n"}
    repo_dir = make_repo(tmp_path / "repo", repo_files, git_env)

    tools = default_registry()
    catalog = AgentCatalog.load(
        tools.names() | STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS, extra_dirs=extra_dirs or ()
    )
    failover = KeyFailover(FakeClock())
    providers_svc = ProvidersService(databases, keystore, model_http, failover)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    workspace_svc = _workspace_svc(paths, databases, keystore)
    store = SessionStore(databases)
    git = GitRunner(paths.bin)
    caller = ModelCaller(providers_svc, model_http, failover)
    workspaces = FakeWorkspaces(workspace=workspace_svc, git=git)
    builder = AgentBuilder(
        catalog=catalog,
        tools=tools,
        settings=settings_svc,
        workspace=workspace_svc,
        workspaces=workspaces,
        store=store,
    )
    executor = TurnExecutor(
        store=store, caller=caller, builder=builder, hooks=stage1_hooks(workspaces, databases)
    )

    prov = await providers_svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake_openai.base_url)
    )
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-tools"))
    await providers_svc.verify_model(f"{prov.name}:fake-tools")

    project = await workspace_svc.add_local(str(repo_dir))
    await settings_svc.set_default_model("orchestrator", f"{prov.name}:fake-tools")

    sid = await store.session_id(project.id, "orchestrator")
    user_msg = await store.add_message(
        project.id,
        sid,
        author_kind="user",
        author_name="User",
        parts=[TextPart(type="text", text="Hello")],
        status="complete",
    )
    turn_id = await store.add_turn(project.id, sid, user_msg.id, "running")

    return (
        executor,
        store,
        settings_svc,
        workspace_svc,
        project.id,
        sid,
        turn_id,
        user_msg.id,
        prov.name,
    )


async def test_executor_refusals(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    (
        executor,
        _store,
        settings_svc,
        _workspace_svc,
        project_id,
        session_id,
        turn_id,
        _,
        _,
    ) = await _setup_executor_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )

    # 1. Project missing refusal
    await _set_project_status(databases, project_id, status="missing", detail="Missing repo")
    sink = RecordingSink()
    status, err = await executor.run(
        project_id, session_id, "orchestrator", turn_id, sink, AsyncRLock()
    )
    assert status == "failed"
    assert err == "Missing repo"
    assert len(sink.messages) == 1
    assert sink.messages[0][0].author_kind == "system"

    # 2. Project empty refusal
    await _set_project_status(databases, project_id, status="empty", detail=None)
    sink = RecordingSink()
    status, err = await executor.run(
        project_id, session_id, "orchestrator", turn_id, sink, AsyncRLock()
    )
    assert status == "done"
    assert err is None
    assert len(sink.messages) == 1
    assert sink.messages[0][0].author_kind == "agent"
    part_0 = sink.messages[0][0].parts[0]
    assert isinstance(part_0, TextPart)
    assert "no committed files yet" in part_0.text

    # 3. Model unassigned refusal
    await _set_project_status(databases, project_id, status="ok", detail=None)
    await settings_svc.set_default_model("orchestrator", None)
    sink = RecordingSink()
    status, err = await executor.run(
        project_id, session_id, "orchestrator", turn_id, sink, AsyncRLock()
    )
    assert status == "failed"
    assert err is not None
    assert "Choose a model" in err
    assert sink.messages[0][0].author_kind == "system"
