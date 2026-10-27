from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import update

from mlops_agents.agents.catalog import AgentCatalog, AgentDefinition
from mlops_agents.agents.runtime.agent import Agent, AgentBuilder, TurnRefused
from mlops_agents.agents.runtime.context import build_messages
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.config import RunMode, Settings, load_settings
from mlops_agents.credentials.keystore import KeyStoreError
from mlops_agents.events.contract import AgentStatus, StatusBoard
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import projects
from mlops_agents.workspace.service import WorkspaceService
from tests.conftest import STAGE_2_TOOLS, MemoryKeyStore, make_repo
from tests.fakes.fake_openai import RunningFake
from tests.fakes.workspaces import FakeWorkspaces
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
    catalog = AgentCatalog.load(tools.names() | STAGE_2_TOOLS)
    providers_svc = ProvidersService(databases, keystore, model_http)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    workspace_svc = _workspace_svc(paths, databases, keystore)
    git = GitRunner(paths.bin)

    workspaces = FakeWorkspaces(workspace=workspace_svc, git=git)
    builder = AgentBuilder(
        catalog=catalog,
        tools=tools,
        settings=settings_svc,
        workspace=workspace_svc,
        workspaces=workspaces,
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
        await builder.build(project_id, "orchestrator")

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
        await builder.build(project_id, "orchestrator")

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
        await builder.build(project_id, "orchestrator")

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
        await builder.build(project_id, "orchestrator")

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
    catalog = AgentCatalog.load(tools.names() | STAGE_2_TOOLS)
    providers_svc = ProvidersService(databases, keystore, model_http)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    workspace_svc = _workspace_svc(paths, databases, keystore)
    git = GitRunner(paths.bin)

    workspaces = FakeWorkspaces(workspace=workspace_svc, git=git)
    builder = AgentBuilder(
        catalog=catalog,
        tools=tools,
        settings=settings_svc,
        workspace=workspace_svc,
        workspaces=workspaces,
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
        await builder.build(project.id, "orchestrator")

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

    agent = await builder.build(project_id, "orchestrator")

    assert isinstance(agent, Agent)
    assert agent.definition.name == "orchestrator"
    # Tools match definition in order for registered tools
    assert tuple(t.name for t in agent.tools) == ("Read", "Glob", "Grep")
    assert agent.project.id == project_id
    assert "README.md" in agent.layout
    assert agent.files.root == workspace_svc.repo_root(agent.project)
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
    catalog = AgentCatalog.load(tools.names() | STAGE_2_TOOLS, extra_dirs=[def_dir])
    providers_svc = ProvidersService(databases, keystore, model_http)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    workspace_svc = _workspace_svc(paths, databases, keystore)
    workspaces = FakeWorkspaces(workspace=workspace_svc, git=GitRunner(paths.bin))
    builder = AgentBuilder(
        catalog=catalog,
        tools=tools,
        settings=settings_svc,
        workspace=workspace_svc,
        workspaces=workspaces,
    )

    prov = await providers_svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake_openai.base_url)
    )
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-tools"))
    await providers_svc.verify_model(f"{prov.name}:fake-tools")

    repo_dir = make_repo(tmp_path / "repo_custom", {"README.md": "Line 1"}, git_env)
    project = await workspace_svc.add_local(str(repo_dir))
    await settings_svc.set_default_model("customagent", f"{prov.name}:fake-tools")

    agent = await builder.build(project.id, "customagent")
    assert agent.definition.name == "customagent"
    assert tuple(t.name for t in agent.tools) == ("Read",)


async def test_repository_only_workspaces_has_no_task_and_the_repository_files(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    workspace = _workspace_svc(paths, databases, keystore)
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "hi\n"}, git_env)
    project = await workspace.add_local(str(repo_dir))
    workspaces = FakeWorkspaces(workspace=workspace, git=GitRunner(paths.bin))

    assert await workspaces.current(project.id, "ses_1") is None
    files = await workspaces.files(project.id, None)

    assert isinstance(files, RepoFiles)
    assert files.root == workspace.repo_root(project)
    assert await files.tracked() == ["README.md"]
    task = TaskRef(id="tsk_1", title="t", branch="b", worktree=tmp_path, status="active")
    with pytest.raises(ValueError, match="tsk_1"):
        await workspaces.files(project.id, task)


class _FakeStatusBoard(StatusBoard):
    def __init__(self, summary_text: str = "- Orchestrator: idle\n- Retraining: running") -> None:
        self.summary_text = summary_text
        self.calls: list[str] = []

    async def snapshot(self, project_id: str) -> list[AgentStatus]:
        return []

    async def summary(self, project_id: str) -> str:
        self.calls.append(project_id)
        return self.summary_text


async def test_orchestrator_system_message_contains_summary_and_other_agent_does_not(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> None:
    d_orch = AgentDefinition(
        name="orchestrator",
        display_name="Orchestrator",
        description="Main assistant",
        tools=("Read",),
        chat=True,
        schedules=False,
        can_message=(),
        context_hooks=(),
        status_board=True,
        prompt="You are the orchestrator.",
    )
    d_other = AgentDefinition(
        name="other",
        display_name="Other Agent",
        description="Other assistant",
        tools=("Read",),
        chat=True,
        schedules=False,
        can_message=(),
        context_hooks=(),
        status_board=False,
        prompt="You are the other agent.",
    )

    tools = default_registry()
    catalog = AgentCatalog([d_orch, d_other], tool_names=tools.names())
    providers_svc = ProvidersService(databases, keystore, model_http)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    workspace_svc = _workspace_svc(paths, databases, keystore)
    workspaces = FakeWorkspaces(workspace=workspace_svc, git=GitRunner(paths.bin))
    fake_board = _FakeStatusBoard(summary_text="- Orchestrator: idle\n- Retraining: running")

    builder = AgentBuilder(
        catalog=catalog,
        tools=tools,
        settings=settings_svc,
        workspace=workspace_svc,
        workspaces=workspaces,
        status_board=fake_board,
    )

    prov = await providers_svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake_openai.base_url)
    )
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-tools"))
    await providers_svc.verify_model(f"{prov.name}:fake-tools")

    repo_dir = make_repo(tmp_path / "repo_sb", {"README.md": "Line 1"}, git_env)
    project = await workspace_svc.add_local(str(repo_dir))
    await settings_svc.set_default_model("orchestrator", f"{prov.name}:fake-tools")
    await settings_svc.set_default_model("other", f"{prov.name}:fake-tools")

    # 1. Build Orchestrator: definition has status_board=True
    orch_agent = await builder.build(project.id, "orchestrator")
    assert orch_agent.status_summary == fake_board.summary_text
    assert fake_board.calls == [project.id]

    # 2. Build Other: definition has status_board=False
    other_agent = await builder.build(project.id, "other")
    assert other_agent.status_summary is None
    # No additional call to summary
    assert fake_board.calls == [project.id]

    # 3. System messages generated with build_messages
    orch_msgs = build_messages(
        orch_agent.definition,
        orch_agent.project,
        orch_agent.layout,
        None,
        [],
        status_summary=orch_agent.status_summary,
    )
    other_msgs = build_messages(
        other_agent.definition,
        other_agent.project,
        other_agent.layout,
        None,
        [],
        status_summary=other_agent.status_summary,
    )

    orch_content = str(orch_msgs[0].content)
    other_content = str(other_msgs[0].content)

    assert "Status board:\n" in orch_content
    assert fake_board.summary_text in orch_content
    assert "Status board:" not in other_content
