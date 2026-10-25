from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx2
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

import tests.fakes.fake_openai as fake_openai_module
from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.agent import AgentBuilder
from mlops_agents.agents.runtime.contract import (
    TextPart,
)
from mlops_agents.agents.runtime.executor import TurnExecutor
from mlops_agents.agents.runtime.hooks import RepositoryOnlyWorkspaces
from mlops_agents.agents.runtime.hub import AsyncRLock
from mlops_agents.agents.runtime.model_caller import ModelCaller
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.settings import AgentLimitsUpdate, AgentSettingsService
from mlops_agents.agents.tools import default_registry
from mlops_agents.config import RunMode, Settings, load_settings
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.sqlite import connect_checkpointer
from mlops_agents.workspace.service import WorkspaceService
from tests.agents.runtime.graph_env import RecordingSink, stage1_hooks
from tests.conftest import MemoryKeyStore, make_repo
from tests.fakes.fake_openai import RunningFake
from tests.integrations.test_github_auth import FakeGh


async def _assert_thread_deleted(db_path: Path, thread_id: str) -> None:
    conn = await connect_checkpointer(db_path)
    saver = AsyncSqliteSaver(conn)
    chk = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})  # pyright: ignore[reportUnknownMemberType]
    assert chk is None
    await conn.close()


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


async def _setup_executor_env(
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
) -> tuple[
    TurnExecutor,
    SessionStore,
    AgentSettingsService,
    WorkspaceService,
    str,
    str,
    str,
    ProvidersService,
    ModelCaller,
]:
    repo_files = files if files is not None else {"README.md": "Line 1 of readme\nLine 2\n"}
    repo_dir = make_repo(tmp_path / "repo", repo_files, git_env)

    tools = default_registry()
    catalog = AgentCatalog.load(tools.names(), extra_dirs=extra_dirs or ())
    providers_svc = ProvidersService(databases, keystore, model_http)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    workspace_svc = _workspace_svc(paths, databases, keystore)
    git = GitRunner(paths.bin)
    store = SessionStore(databases)
    caller = ModelCaller(providers_svc, model_http)
    workspaces = RepositoryOnlyWorkspaces(workspace=workspace_svc, git=git)
    builder = AgentBuilder(
        catalog=catalog,
        tools=tools,
        settings=settings_svc,
        workspace=workspace_svc,
        workspaces=workspaces,
    )
    executor = TurnExecutor(
        store=store, caller=caller, builder=builder, hooks=stage1_hooks(workspaces)
    )

    prov = await providers_svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake.base_url)
    )
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-tools"))
    await providers_svc.verify_model(f"{prov.name}:fake-tools")

    project = await workspace_svc.add_local(str(repo_dir))
    await settings_svc.set_default_model("orchestrator", f"{prov.name}:fake-tools")
    session_id = await store.session_id(project.id, "orchestrator")

    user_msg = await store.add_message(
        project.id,
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Check readme")],
        status="complete",
    )
    turn_id = await store.add_turn(project.id, session_id, user_msg.id, "running")

    return (
        executor,
        store,
        settings_svc,
        workspace_svc,
        project.id,
        session_id,
        turn_id,
        providers_svc,
        caller,
    )


async def test_executor_fake_tools_on_orchestrator(
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
        _,
        _,
        project_id,
        session_id,
        turn_id,
        _,
        _,
    ) = await _setup_executor_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    sink = RecordingSink()

    end_status, error_text = await executor.run(
        project_id, session_id, "orchestrator", turn_id, sink, AsyncRLock()
    )

    assert end_status == "done"
    assert error_text is None

    # Check sink events
    assert len(sink.messages) == 2  # created=True and created=False
    assert sink.messages[0][1] is True
    assert sink.messages[1][1] is False
    assert len(sink.tools_started) == 1
    assert sink.tools_started[0].name == "Read"
    assert len(sink.tools_finished) == 1
    assert not sink.tools_finished[0].is_error
    assert len(sink.texts) > 0

    # Parts order: tool call, tool result, final text
    final_msg = sink.messages[1][0]
    types = [p.type for p in final_msg.parts]
    assert types == ["tool_call", "tool_result", "text"]

    # Final text contains the first line
    final_text_part = final_msg.parts[2]
    assert isinstance(final_text_part, TextPart)
    assert "Line 1 of readme" in final_text_part.text

    # llm_calls has 2 rows with latency > 0
    engine = await databases.project(project_id)
    from sqlalchemy import select

    from mlops_agents.storage.tables import llm_calls

    async with engine.connect() as conn:
        rows = (await conn.execute(select(llm_calls).where(llm_calls.c.turn_id == turn_id))).all()
    assert len(rows) == 2
    for r in rows:
        assert r.latency_ms >= 0

    # Verify checkpointer thread is deleted after done turn
    await _assert_thread_deleted(paths.project_db(project_id), turn_id)


async def test_executor_secret_and_untracked_error_handled(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Repo with .env
    (
        executor,
        _store,
        _,
        _,
        project_id,
        session_id,
        turn_id,
        _,
        _,
    ) = await _setup_executor_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        files={"README.md": "Hello", ".env": "SECRET=123"},
    )
    # 1. Direct fake model to call Read on .env (tracked secret)
    monkeypatch.setitem(fake_openai_module.TOOL_ARGS, "Read", {"file_path": ".env"})

    sink = RecordingSink()
    end_status, error_text = await executor.run(
        project_id, session_id, "orchestrator", turn_id, sink, AsyncRLock()
    )

    assert end_status == "done"
    assert error_text is None
    # Tool finished reported error
    assert len(sink.tools_finished) == 1
    assert sink.tools_finished[0].is_error

    # 2. Untracked file: create untracked.txt in the repo and call Read on it
    repo_dir = tmp_path / "repo"
    (repo_dir / "untracked.txt").write_text("untracked content")
    monkeypatch.setitem(fake_openai_module.TOOL_ARGS, "Read", {"file_path": "untracked.txt"})

    user_msg2 = await _store.add_message(
        project_id,
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Read untracked")],
        status="complete",
    )
    turn_id2 = await _store.add_turn(project_id, session_id, user_msg2.id, "running")
    sink2 = RecordingSink()
    end_status2, error_text2 = await executor.run(
        project_id, session_id, "orchestrator", turn_id2, sink2, AsyncRLock()
    )

    assert end_status2 == "done"
    assert error_text2 is None
    assert len(sink2.tools_finished) == 1
    assert sink2.tools_finished[0].is_error


async def test_executor_step_limit_override(
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
        _,
        project_id,
        session_id,
        turn_id,
        _,
        _,
    ) = await _setup_executor_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    # Override max_steps = 1
    await settings_svc.set_project_limits(
        project_id, "orchestrator", AgentLimitsUpdate(max_steps=1)
    )

    sink = RecordingSink()
    end_status, _error_text = await executor.run(
        project_id, session_id, "orchestrator", turn_id, sink, AsyncRLock()
    )

    assert end_status == "done"
    final_msg = sink.messages[-1][0]
    last_part = final_msg.parts[-1]
    assert isinstance(last_part, TextPart)
    assert (
        last_part.text
        == "Stopped after 1 steps (this agent's step limit). Send a message to continue."
    )
