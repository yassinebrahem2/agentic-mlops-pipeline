from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import update

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.agent import Agent, AgentBuilder, TurnRefused
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools import default_registry
from mlops_agents.config import RunMode, Settings, load_settings
from mlops_agents.credentials.keystore import KeyStoreError
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import projects
from mlops_agents.workspace.service import WorkspaceService
from tests.conftest import MemoryKeyStore, make_repo
from tests.fakes.fake_openai import RunningFake
from tests.integrations.test_github_auth import FakeGh


async def _set_project_status(
    databases: Databases, project_id: str, status: str, detail: str | None = None
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
        _settings(paths), paths, databases, GitRunner(paths.bin, github.token), github, http
    )


async def _setup_services(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> tuple[AgentBuilder, WorkspaceService, AgentSettingsService, str]:
    tools = default_registry()
    catalog = AgentCatalog.load(tools.names())
    providers_svc = ProvidersService(databases, keystore, model_http)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    workspace_svc = _workspace_svc(paths, databases, keystore)
    git = GitRunner(paths.bin)

    builder = AgentBuilder(
        catalog=catalog,
        tools=tools,
        settings=settings_svc,
        workspace=workspace_svc,
        git=git,
    )

    prov = await providers_svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake.base_url)
    )
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-tools"))
    await providers_svc.verify_model(f"{prov.name}:fake-tools")

    repo_dir = make_repo(tmp_path / "test_repo", {"README.md": "# Hello\nWorld\n"}, git_env)
    project = await workspace_svc.add_local(str(repo_dir))
    await settings_svc.set_default_model("orchestrator", f"{prov.name}:fake-tools")

    return builder, workspace_svc, settings_svc, project.id


async def test_agent_builder_missing_project_refusal(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    builder, _, _, project_id = await _setup_services(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    # Mark project as missing
    await _set_project_status(databases, project_id, status="missing", detail="Folder deleted")

    with pytest.raises(TurnRefused) as exc_info:
        await builder.build(project_id, "orchestrator", session_id="ses_t", turn_id="trn_t")

    assert exc_info.value.end == "failed"
    assert exc_info.value.text == "Folder deleted"


async def test_agent_builder_clone_failed_project_refusal(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    builder, _, _, project_id = await _setup_services(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    await _set_project_status(
        databases,
        project_id,
        status="clone_failed",
        detail="Network error cloning repository",
    )

    with pytest.raises(TurnRefused) as exc_info:
        await builder.build(project_id, "orchestrator", session_id="ses_t", turn_id="trn_t")

    assert exc_info.value.end == "failed"
    assert exc_info.value.text == "Network error cloning repository"


async def test_agent_builder_empty_project_refusal(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    builder, _, _, project_id = await _setup_services(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    await _set_project_status(databases, project_id, status="empty", detail=None)

    with pytest.raises(TurnRefused) as exc_info:
        await builder.build(project_id, "orchestrator", session_id="ses_t", turn_id="trn_t")

    assert exc_info.value.end == "done"
    assert (
        exc_info.value.text
        == "This repository has no committed files yet. Commit your files, then ask again."
    )


async def test_agent_builder_no_model_assigned_refusal(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    builder, _, settings_svc, project_id = await _setup_services(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    # Remove default model
    await settings_svc.set_default_model("orchestrator", None)

    with pytest.raises(TurnRefused) as exc_info:
        await builder.build(project_id, "orchestrator", session_id="ses_t", turn_id="trn_t")

    assert exc_info.value.end == "failed"
    assert "Choose a model" in exc_info.value.text


async def test_agent_builder_keystore_error_refusal(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = default_registry()
    catalog = AgentCatalog.load(tools.names())
    providers_svc = ProvidersService(databases, keystore, model_http)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    workspace_svc = _workspace_svc(paths, databases, keystore)
    git = GitRunner(paths.bin)

    builder = AgentBuilder(
        catalog=catalog,
        tools=tools,
        settings=settings_svc,
        workspace=workspace_svc,
        git=git,
    )

    prov = await providers_svc.create_provider(
        ProviderCreate(name="groq", kind="hosted", base_url=fake_openai.base_url)
    )
    await providers_svc.add_key("groq", "test-key-123")
    await providers_svc.add_model(
        ModelCreate(provider=prov.name, wire_model_id="fake-tools", context_window=32768)
    )
    await providers_svc.verify_model(f"{prov.name}:fake-tools")

    repo_dir = make_repo(tmp_path / "test_repo", {"README.md": "# Hello\nWorld\n"}, git_env)
    project = await workspace_svc.add_local(str(repo_dir))
    await settings_svc.set_default_model("orchestrator", f"{prov.name}:fake-tools")

    def raise_keystore(*args: object, **kwargs: object) -> None:
        raise KeyStoreError("Keyring is locked")

    monkeypatch.setattr(keystore, "get", raise_keystore)

    with pytest.raises(TurnRefused) as exc_info:
        await builder.build(project.id, "orchestrator", session_id="ses_t", turn_id="trn_t")

    assert exc_info.value.end == "failed"
    assert "Keyring is locked" in exc_info.value.text


async def test_agent_builder_success(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    builder, workspace_svc, _, project_id = await _setup_services(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )

    agent = await builder.build(project_id, "orchestrator", session_id="ses_t", turn_id="trn_t")

    assert isinstance(agent, Agent)
    assert agent.definition.name == "orchestrator"
    # Tools match definition in order
    assert tuple(t.name for t in agent.tools) == agent.definition.tools
    assert agent.project.id == project_id
    assert "README.md" in agent.layout
    assert agent.tool_context.files.root == workspace_svc.repo_root(agent.project)
    context = agent.tool_context
    assert (context.project_id, context.agent) == (project_id, "orchestrator")
    assert (context.session_id, context.turn_id, context.task) == ("ses_t", "trn_t", None)
    assert agent.model.wire_model_id == "fake-tools"


async def test_agent_builder_custom_agent_in_extra_dir(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    def_dir = tmp_path / "extra_defs"
    def_dir.mkdir()
    def_file = def_dir / "customagent.md"
    def_file.write_text(
        "---\nname: customagent\ndisplay_name: Custom Agent\ndescription: Test custom agent\ntools:\n  - Read\n---\nYou are custom agent.\n"
    )

    tools = default_registry()
    catalog = AgentCatalog.load(tools.names(), extra_dirs=[def_dir])
    providers_svc = ProvidersService(databases, keystore, model_http)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    workspace_svc = _workspace_svc(paths, databases, keystore)
    builder = AgentBuilder(
        catalog=catalog,
        tools=tools,
        settings=settings_svc,
        workspace=workspace_svc,
        git=GitRunner(paths.bin),
    )

    prov = await providers_svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake_openai.base_url)
    )
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-tools"))
    await providers_svc.verify_model(f"{prov.name}:fake-tools")

    repo_dir = make_repo(tmp_path / "repo_custom", {"README.md": "Line 1"}, git_env)
    project = await workspace_svc.add_local(str(repo_dir))
    await settings_svc.set_default_model("customagent", f"{prov.name}:fake-tools")

    agent = await builder.build(project.id, "customagent", session_id="ses_t", turn_id="trn_t")
    assert agent.definition.name == "customagent"
    assert tuple(t.name for t in agent.tools) == ("Read",)
