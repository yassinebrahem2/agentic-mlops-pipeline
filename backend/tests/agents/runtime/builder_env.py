"""One real AgentBuilder against the fake model server, shared by the builder tests."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import httpx2

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.hooks.registry import HookRegistry
from mlops_agents.agents.hooks.status_board import StatusBoardHook
from mlops_agents.agents.runtime.agent import AgentBuilder
from mlops_agents.agents.runtime.contract import TextPart
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools import default_registry
from mlops_agents.config import RunMode, Settings, load_settings
from mlops_agents.events.contract import StatusBoard
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.failover import KeyFailover
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.workspace.service import WorkspaceService
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


@dataclass(frozen=True)
class BuilderEnv:
    builder: AgentBuilder
    workspace: WorkspaceService
    settings: AgentSettingsService
    store: SessionStore
    project_id: str
    provider: str


def _settings(paths: DataPaths) -> Settings:
    base = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)})
    return replace(base, mode=RunMode.LOCAL)


def workspace_service(
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


def status_board_hooks(board: StatusBoard) -> HookRegistry:
    """A hook registry holding the status_board hook, bound to `board`."""
    hook = StatusBoardHook()
    hook.bind(board)
    hooks = HookRegistry()
    hooks.register(hook)
    return hooks


async def start_turn(
    store: SessionStore, project_id: str, agent: str, *texts: str
) -> tuple[str, str]:
    """A stored trigger message (one text part per text) and its running turn: (session, turn)."""
    session_id = await store.session_id(project_id, agent)
    trigger = await store.add_message(
        project_id,
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text=t) for t in texts],
        status="complete",
    )
    return session_id, await store.add_turn(project_id, session_id, trigger.id, "running")


async def build_builder_env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    *,
    hooks: HookRegistry | None = None,
    extra_dirs: Sequence[Path] = (),
) -> BuilderEnv:
    tools = default_registry()
    hook_names: frozenset[str] = STAGE_3_HOOKS if hooks is None else (STAGE_3_HOOKS | hooks.names())
    catalog = AgentCatalog.load(
        tools.names() | STAGE_2_TOOLS,
        extra_dirs,
        hook_names=hook_names,
    )
    providers_svc = ProvidersService(databases, keystore, model_http, KeyFailover(FakeClock()))
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    workspace_svc = workspace_service(paths, databases, keystore)
    store = SessionStore(databases)
    builder = AgentBuilder(
        catalog=catalog,
        tools=tools,
        settings=settings_svc,
        workspace=workspace_svc,
        workspaces=FakeWorkspaces(workspace=workspace_svc, git=GitRunner(paths.bin)),
        store=store,
        hooks=hooks,
    )

    prov = await providers_svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake.base_url)
    )
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-tools"))
    await providers_svc.verify_model(f"{prov.name}:fake-tools")

    repo_dir = make_repo(tmp_path / "test_repo", {"README.md": "# Hello\nWorld\n"}, git_env)
    project = await workspace_svc.add_local(str(repo_dir))
    await settings_svc.set_default_model("orchestrator", f"{prov.name}:fake-tools")

    return BuilderEnv(
        builder=builder,
        workspace=workspace_svc,
        settings=settings_svc,
        store=store,
        project_id=project.id,
        provider=prov.name,
    )
