from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools import ToolRegistry, default_registry
from mlops_agents.config import RunMode, Settings, load_settings
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
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

    with pytest.raises(DomainError) as exc_404_stop:
        await manager.stop(project_id, "nonexistent")
    assert exc_404_stop.value.status == 404

    with pytest.raises(DomainError) as exc_404_sub:
        sub = manager.subscribe(project_id, "nonexistent")
        await anext(sub)
    assert exc_404_sub.value.status == 404

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
