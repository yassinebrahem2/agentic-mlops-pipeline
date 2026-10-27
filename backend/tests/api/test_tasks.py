from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.contract import ToolFinished
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tasks.service import TasksService
from mlops_agents.agents.tools import default_registry
from mlops_agents.config import RunMode, load_settings
from mlops_agents.events.audit import SqliteAuditLog
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.events.status import SqliteStatusBoard
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import tasks
from mlops_agents.workspace.service import WorkspaceService
from tests.agents.runtime.hook_fakes import NoInbound, ScriptedGate
from tests.agents.runtime.session_env import collect_until_turn_ended
from tests.api.conftest import add_local_project
from tests.conftest import STAGE_2_TOOLS, MemoryKeyStore, make_repo
from tests.fakes.fake_openai import RunningFake
from tests.fakes.turn_end import NoTurnEndCheck
from tests.integrations.test_github_auth import FakeGh


@pytest.fixture(autouse=True)
def _suppress_docker_cleanup() -> Any:
    with patch(
        "mlops_agents.sandbox.containers.ContainerManager.cleanup_all",
        new=AsyncMock(),
    ):
        yield


async def test_discard_task_endpoint_success(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services = app.state.services

    # Create session and start a task
    session = await services.sessions.get(project_id, "orchestrator")
    task = await services.tasks.start(project_id, session.id, "orchestrator", "Task to discard")
    assert task.worktree.exists()

    res = await client.post(f"/api/projects/{project_id}/tasks/{task.id}/discard")
    assert res.status_code == 204

    # Verify task ended and status is discarded
    engine = await services.databases.project(project_id)
    async with engine.connect() as conn:
        row = (await conn.execute(select(tasks).where(tasks.c.id == task.id))).mappings().first()
        assert row is not None
        assert row["status"] == "discarded"
        assert row["ended_at"] is not None

    # Worktree removed
    assert not task.worktree.exists()


async def test_discard_task_endpoint_conflict_while_running_or_waiting(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services = app.state.services

    session = await services.sessions.get(project_id, "orchestrator")
    task = await services.tasks.start(project_id, session.id, "orchestrator", "Busy task")

    # Mock turn_states to simulate running turn for orchestrator
    with patch.object(services.sessions, "turn_states", return_value={"orchestrator": "running"}):
        res = await client.post(f"/api/projects/{project_id}/tasks/{task.id}/discard")
        assert res.status_code == 409

    # Mock turn_states to simulate waiting turn for orchestrator
    with patch.object(services.sessions, "turn_states", return_value={"orchestrator": "waiting"}):
        res = await client.post(f"/api/projects/{project_id}/tasks/{task.id}/discard")
        assert res.status_code == 409


async def test_discard_task_global_endpoint(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services = app.state.services

    session = await services.sessions.get(project_id, "orchestrator")
    task = await services.tasks.start(project_id, session.id, "orchestrator", "Global discard task")

    # Call /api/tasks/{task_id}/discard without project_id in path
    res = await client.post(f"/api/tasks/{task.id}/discard")
    assert res.status_code == 204

    engine = await services.databases.project(project_id)
    async with engine.connect() as conn:
        row = (await conn.execute(select(tasks).where(tasks.c.id == task.id))).mappings().first()
        assert row is not None
        assert row["status"] == "discarded"


async def test_workspace_remove_prunes_worktrees_and_deletes_agent_branches(
    app: FastAPI, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services = app.state.services
    project = await services.workspace.get(project_id)
    repo = services.workspace.repo_root(project)

    session = await services.sessions.get(project_id, "orchestrator")
    task = await services.tasks.start(project_id, session.id, "orchestrator", "Task for prune test")
    assert task.worktree.exists()

    # Verify agent branch exists in repo
    runner = GitRunner(services.paths.bin, services.github.token)
    branch_check = await runner.run(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/agent"], cwd=repo
    )
    assert task.branch in branch_check.stdout

    # Remove project via WorkspaceService
    await services.workspace.remove(project_id)

    # Worktrees are pruned, and agent/* branches are deleted in repo
    after_branches = await runner.run(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/agent"], cwd=repo
    )
    assert not after_branches.stdout.strip()


async def test_wired_session_start_task_write_and_read_later_turn(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    repo = make_repo(tmp_path / "repo", {"hello.txt": "initial\n"}, git_env)
    http = httpx2.AsyncClient()
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    runner = GitRunner(paths.bin, github.token)
    app_settings = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)})
    workspace = WorkspaceService(
        app_settings,
        paths,
        databases,
        runner,
        github,
        http,
    )
    events = SqliteEventLog(databases)
    _audit = SqliteAuditLog(databases, secrets=lambda: [])
    project = await workspace.add_local(str(repo))

    tasks_svc = TasksService(
        databases=databases,
        paths=paths,
        git=runner,
        events=events,
        workspace=workspace,
    )

    tools = default_registry(tasks=tasks_svc)
    catalog = AgentCatalog.load(tools.names() | STAGE_2_TOOLS)
    base_def = catalog.get("orchestrator")
    custom_def = base_def.model_copy(update={"tools": ("StartTask", "Write", "Read")})
    catalog = AgentCatalog(
        [custom_def if d.name == "orchestrator" else d for d in catalog.all()],
        tool_names=tools.names() | STAGE_2_TOOLS,
    )
    providers_svc = ProvidersService(databases, keystore, model_http)
    agent_settings = AgentSettingsService(databases, providers_svc, catalog)

    prov = await providers_svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake_openai.base_url)
    )
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-tools"))
    await providers_svc.verify_model(f"{prov.name}:fake-tools")
    await agent_settings.set_default_model("orchestrator", f"{prov.name}:fake-tools")

    status_board = SqliteStatusBoard(catalog, events)
    session_mgr = SessionManager(
        databases=databases,
        workspace=workspace,
        catalog=catalog,
        tools=tools,
        settings=agent_settings,
        providers=providers_svc,
        http=model_http,
        gate=ScriptedGate(),
        inbound=NoInbound(),
        turn_end=NoTurnEndCheck(),
        workspaces=tasks_svc,
        status_board=status_board,
    )

    session_view = await session_mgr.get(project.id, "orchestrator")
    assert session_view.current_task is None

    # Step 1: Turn 1 starts task and writes a file (2 steps in one turn)
    # The model will call StartTask, then Write
    fake_openai.state.scripted_tool_calls = [
        {"id": "call_1", "name": "StartTask", "arguments": {"title": "Wired task"}},
    ]
    sub = session_mgr.subscribe(project.id, "orchestrator", after_seq=session_view.last_seq)
    await session_mgr.send(project.id, "orchestrator", "Start a task")
    await collect_until_turn_ended(sub)

    # Verify task was started
    current = await tasks_svc.current(project.id, session_view.id)
    assert current is not None
    assert current.worktree.exists()

    # Check session view now has current_task
    updated_session = await session_mgr.get(project.id, "orchestrator")
    assert updated_session.current_task is not None
    assert updated_session.current_task.id == current.id

    # Step 2: Write into the task worktree
    fake_openai.state.scripted_tool_calls = [
        {
            "id": "call_2",
            "name": "Write",
            "arguments": {"file_path": "wired.py", "content": "print('wired')\n"},
        },
    ]
    sub2 = session_mgr.subscribe(project.id, "orchestrator", after_seq=updated_session.last_seq)
    await session_mgr.send(project.id, "orchestrator", "Write wired.py")
    await collect_until_turn_ended(sub2)

    # Verify wired.py is in task worktree, but NOT in repo root
    assert (current.worktree / "wired.py").exists()
    assert (current.worktree / "wired.py").read_text(encoding="utf-8") == "print('wired')\n"
    assert not (repo / "wired.py").exists()

    # Step 3: Later turn calls Read on wired.py and sees it
    fake_openai.state.scripted_tool_calls = [
        {"id": "call_3", "name": "Read", "arguments": {"file_path": "wired.py"}},
    ]
    sub3 = session_mgr.subscribe(project.id, "orchestrator", after_seq=None)
    # Drain initial resync
    await anext(sub3)
    await session_mgr.send(project.id, "orchestrator", "Read wired.py")
    events_t3 = await collect_until_turn_ended(sub3)

    # Check tool output from tool call
    tool_finished_events = [e for e in events_t3 if isinstance(e, ToolFinished)]
    assert len(tool_finished_events) == 1
    assert "print('wired')" in tool_finished_events[0].data.content
