from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, ClassVar

import httpx2
import pytest
from pydantic import BaseModel

from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.service import ApprovalsService
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.contract import (
    ApprovalPart,
    StreamEvent,
    ToolResultPart,
    TurnEnded,
    TurnWaiting,
)
from mlops_agents.agents.runtime.hooks import (
    NoInbound,
)
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.agents.tools import ToolRegistry, default_registry
from mlops_agents.agents.tools.base import (
    AgentTool,
    FileAccess,
    Preview,
    RiskClass,
    TaskRef,
    ToolContext,
    ToolOutput,
)
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.config import RunMode, load_settings
from mlops_agents.events.audit import SqliteAuditLog
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import build_session_env
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.fakes.turn_end import NoTurnEndCheck
from tests.fakes.workspaces import FakeWorkspaces


class DummyArgs(BaseModel):
    file_path: str = "README.md"
    content: str = "new content"


class DummyWriteTool(AgentTool[DummyArgs]):
    name: ClassVar[str] = "write_code_tool"
    description: ClassVar[str] = "Dummy write tool for approvals"
    risk: ClassVar[RiskClass] = RiskClass.WRITE_CODE
    args_model: ClassVar[type[BaseModel]] = DummyArgs

    def __init__(self) -> None:
        self.invoked: list[tuple[ToolContext, DummyArgs]] = []

    async def run(self, ctx: ToolContext, args: DummyArgs) -> ToolOutput:
        self.invoked.append((ctx, args))
        return ToolOutput("File written successfully.")

    async def preview(self, ctx: ToolContext, args: DummyArgs) -> Preview:
        return Preview(kind="diff", title="write_code_tool", body="+ new content")


class DummyRunTool(AgentTool[DummyArgs]):
    name: ClassVar[str] = "run_bash_tool"
    description: ClassVar[str] = "Dummy run tool for approvals"
    risk: ClassVar[RiskClass] = RiskClass.RUN
    args_model: ClassVar[type[BaseModel]] = DummyArgs

    def __init__(self) -> None:
        self.invoked: list[tuple[ToolContext, DummyArgs]] = []

    async def run(self, ctx: ToolContext, args: DummyArgs) -> ToolOutput:
        self.invoked.append((ctx, args))
        return ToolOutput("Command executed successfully.")

    async def preview(self, ctx: ToolContext, args: DummyArgs) -> Preview:
        return Preview(kind="command", title="run_bash_tool", body="echo hello")


def make_tool_reg(*extra_tools: AgentTool[Any]) -> ToolRegistry:
    reg = default_registry()
    registered: set[str] = set()
    for t in extra_tools:
        reg.register(t)
        registered.add(t.name)
    if "write_code_tool" not in registered:
        reg.register(DummyWriteTool())
    if "run_bash_tool" not in registered:
        reg.register(DummyRunTool())
    return reg


class MutableTaskWorkspaces(FakeWorkspaces):
    """Workspaces double allowing testing task changes between approval and execution."""

    def __init__(self, *, workspace: Any, git: Any) -> None:
        super().__init__(workspace=workspace, git=git)
        self.current_task_ref: TaskRef | None = None

    async def current(self, project_id: str, session_id: str) -> TaskRef | None:
        return self.current_task_ref

    async def files(self, project_id: str, task: TaskRef | None) -> FileAccess:
        if task is not None:
            return RepoFiles(task.worktree, self._git)
        return await super().files(project_id, task)


def _write_agent_def(tmp_path: Path) -> Path:
    def_dir = tmp_path / "definitions"
    def_dir.mkdir(parents=True, exist_ok=True)
    (def_dir / "writer.md").write_text(
        "---\nname: writer\ndisplay_name: Writer\ndescription: Test agent\n"
        "chat: true\ntools:\n  - write_code_tool\n  - run_bash_tool\n---\nPrompt\n",
        encoding="utf-8",
    )
    return def_dir


async def collect_until_turn_waiting(
    sub: AsyncGenerator[StreamEvent, None],
    timeout: float = 10.0,  # noqa: ASYNC109
) -> list[StreamEvent]:
    events: list[StreamEvent] = []

    async def collector() -> None:
        while True:
            ev = await anext(sub)
            events.append(ev)
            if isinstance(ev, TurnWaiting):
                break

    await asyncio.wait_for(collector(), timeout=timeout)
    return events


async def collect_until_turn_ended(
    sub: AsyncGenerator[StreamEvent, None],
    timeout: float = 10.0,  # noqa: ASYNC109
) -> list[StreamEvent]:
    events: list[StreamEvent] = []

    async def collector() -> None:
        while True:
            ev = await anext(sub)
            events.append(ev)
            if isinstance(ev, TurnEnded):
                break

    await asyncio.wait_for(collector(), timeout=timeout)
    return events


@pytest.mark.asyncio
async def test_resuming_turn_approve(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    """1. Approve: Tool runs, turn continues and ends done, message keeps its earlier parts."""
    write_tool = DummyWriteTool()
    tool_reg = make_tool_reg(write_tool)
    def_dir = _write_agent_def(tmp_path)

    audit_log = SqliteAuditLog(databases, secrets=[])
    event_log = SqliteEventLog(databases)
    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store, audit_log=audit_log, event_log=event_log)

    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        extra_dirs=[def_dir],
        tools=tool_reg,
        gate=gate,
    )
    manager = env.manager
    project_id = env.project_id
    await env.settings.set_default_model("writer", f"{env.provider}:fake-tools")

    approvals_service = ApprovalsService(
        databases=databases,
        sessions=manager,
        audit_log=audit_log,
        event_log=event_log,
    )

    fake_openai.state.scripted_tool_calls = [
        {"name": "write_code_tool", "arguments": {"file_path": "a.py", "content": "print(1)"}}
    ]

    sub = manager.subscribe(project_id, "writer", after_seq=None)
    await anext(sub)

    await manager.send(project_id, "writer", "Please write a.py")
    await collect_until_turn_waiting(sub)

    # Verify pending approval
    pending = await approvals_service.pending(project_id)
    assert len(pending) == 1
    apr = pending[0]
    assert apr.tool == "write_code_tool"
    assert apr.status == "pending"

    # Now approve
    await approvals_service.decide(project_id, apr.id, approve=True, note="Go ahead")

    # Wait for turn to resume and finish
    end_events = await collect_until_turn_ended(sub)
    turn_ended = next(e for e in end_events if isinstance(e, TurnEnded))
    assert turn_ended.data.status == "done"

    # Tool actually executed
    assert len(write_tool.invoked) == 1

    # Check message has all parts: text, tool call, approval part, tool result, and final text
    session_view = await manager.get(project_id, "writer")
    agent_msg = [m for m in session_view.messages if m.author_kind == "agent"][-1]
    assert agent_msg.status == "complete"
    part_types = [p.type for p in agent_msg.parts]
    assert "tool_call" in part_types
    assert "approval" in part_types
    assert "tool_result" in part_types

    apr_part = next(p for p in agent_msg.parts if isinstance(p, ApprovalPart))
    assert apr_part.status == "approved"
    assert apr_part.note == "Go ahead"

    res_part = next(p for p in agent_msg.parts if isinstance(p, ToolResultPart))
    assert res_part.is_error is False
    assert "File written successfully" in res_part.content

    await manager.close()


@pytest.mark.asyncio
async def test_resuming_turn_reject_with_note(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    """2. Reject with a note: Model receives the rejection and note."""
    write_tool = DummyWriteTool()
    tool_reg = make_tool_reg(write_tool)
    def_dir = _write_agent_def(tmp_path)

    audit_log = SqliteAuditLog(databases, secrets=[])
    event_log = SqliteEventLog(databases)
    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store, audit_log=audit_log, event_log=event_log)

    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        extra_dirs=[def_dir],
        tools=tool_reg,
        gate=gate,
    )
    manager = env.manager
    project_id = env.project_id
    await env.settings.set_default_model("writer", f"{env.provider}:fake-tools")

    approvals_service = ApprovalsService(
        databases=databases,
        sessions=manager,
        audit_log=audit_log,
        event_log=event_log,
    )

    fake_openai.state.scripted_tool_calls = [
        {"name": "write_code_tool", "arguments": {"file_path": "a.py", "content": "bad code"}}
    ]

    sub = manager.subscribe(project_id, "writer", after_seq=None)
    await anext(sub)

    await manager.send(project_id, "writer", "Write something")
    await collect_until_turn_waiting(sub)

    pending = await approvals_service.pending(project_id)
    assert len(pending) == 1
    apr = pending[0]

    # Reject with note
    await approvals_service.decide(project_id, apr.id, approve=False, note="This is unsafe")

    # Turn resumes and ends done
    end_events = await collect_until_turn_ended(sub)
    turn_ended = next(e for e in end_events if isinstance(e, TurnEnded))
    assert turn_ended.data.status == "done"

    # Tool did NOT run
    assert len(write_tool.invoked) == 0

    # Message contains rejected tool_result with rejection note
    session_view = await manager.get(project_id, "writer")
    agent_msg = [m for m in session_view.messages if m.author_kind == "agent"][-1]
    res_part = next(p for p in agent_msg.parts if isinstance(p, ToolResultPart))
    assert res_part.is_error is True
    assert "The user rejected this action" in res_part.content
    assert "This is unsafe" in res_part.content

    await manager.close()


@pytest.mark.asyncio
async def test_resuming_turn_two_asks_in_one_step(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    """3. Two asks in one step:
    - Turn resumes only after both are decided.
    - Concurrent decisions: both parts show decision and turn resumes cleanly.
    """
    write_tool = DummyWriteTool()
    run_tool = DummyRunTool()
    tool_reg = make_tool_reg(write_tool, run_tool)
    def_dir = _write_agent_def(tmp_path)

    audit_log = SqliteAuditLog(databases, secrets=[])
    event_log = SqliteEventLog(databases)
    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store, audit_log=audit_log, event_log=event_log)

    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        extra_dirs=[def_dir],
        tools=tool_reg,
        gate=gate,
    )
    manager = env.manager
    project_id = env.project_id
    await env.settings.set_default_model("writer", f"{env.provider}:fake-tools")

    approvals_service = ApprovalsService(
        databases=databases,
        sessions=manager,
        audit_log=audit_log,
        event_log=event_log,
    )

    fake_openai.state.scripted_tool_calls = [
        {"name": "write_code_tool", "arguments": {"file_path": "a.py"}},
        {"name": "run_bash_tool", "arguments": {"file_path": "b.py"}},
    ]

    sub = manager.subscribe(project_id, "writer", after_seq=None)
    await anext(sub)

    await manager.send(project_id, "writer", "Do both")
    await collect_until_turn_waiting(sub)

    pending = await approvals_service.pending(project_id)
    assert len(pending) == 2

    # Concurrently decide both
    await asyncio.gather(
        approvals_service.decide(project_id, pending[0].id, approve=True, note="OK 1"),
        approvals_service.decide(project_id, pending[1].id, approve=True, note="OK 2"),
    )

    end_events = await collect_until_turn_ended(sub)
    turn_ended = next(e for e in end_events if isinstance(e, TurnEnded))
    assert turn_ended.data.status == "done"

    assert len(write_tool.invoked) == 1
    assert len(run_tool.invoked) == 1

    session_view = await manager.get(project_id, "writer")
    agent_msg = [m for m in session_view.messages if m.author_kind == "agent"][-1]
    apr_parts = [p for p in agent_msg.parts if isinstance(p, ApprovalPart)]
    assert len(apr_parts) == 2
    assert apr_parts[0].status == "approved"
    assert apr_parts[1].status == "approved"

    await manager.close()


@pytest.mark.asyncio
async def test_resuming_turn_after_rebuilding_manager(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    """4. Approve after rebuilding session manager:
    Turn resumes from stored checkpoint across restart, keeping earlier parts.
    """
    write_tool = DummyWriteTool()
    tool_reg = make_tool_reg(write_tool)
    def_dir = _write_agent_def(tmp_path)

    audit_log = SqliteAuditLog(databases, secrets=[])
    event_log = SqliteEventLog(databases)
    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store, audit_log=audit_log, event_log=event_log)

    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        extra_dirs=[def_dir],
        tools=tool_reg,
        gate=gate,
    )
    manager = env.manager
    project_id = env.project_id
    await env.settings.set_default_model("writer", f"{env.provider}:fake-tools")

    fake_openai.state.scripted_tool_calls = [
        {"name": "write_code_tool", "arguments": {"file_path": "restart.py"}}
    ]

    sub = manager.subscribe(project_id, "writer", after_seq=None)
    await anext(sub)

    await manager.send(project_id, "writer", "Write restart file")
    await collect_until_turn_waiting(sub)

    pending = await store.pending(project_id)
    assert len(pending) == 1
    apr_id = pending[0].id

    # Simulate app shutdown and restart: close session manager
    await manager.close()

    # Rebuild session manager from same databases
    from mlops_agents.agents.catalog import AgentCatalog
    from mlops_agents.agents.settings import AgentSettingsService
    from mlops_agents.integrations.github.auth import GitHubAuth
    from mlops_agents.integrations.github.git import GitRunner
    from mlops_agents.providers.service import ProvidersService
    from mlops_agents.workspace.service import WorkspaceService
    from tests.integrations.test_github_auth import FakeGh

    catalog = AgentCatalog.load(tool_reg.names(), extra_dirs=[def_dir])
    providers_svc = ProvidersService(databases, keystore, model_http)
    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    http = httpx2.AsyncClient()
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    settings = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)})
    workspace_svc = WorkspaceService(
        settings, paths, databases, GitRunner(paths.bin, github.token), github, http
    )
    new_manager = SessionManager(
        databases=databases,
        workspace=workspace_svc,
        catalog=catalog,
        tools=tool_reg,
        settings=settings_svc,
        providers=providers_svc,
        http=model_http,
        gate=gate,
        inbound=NoInbound(),
        turn_end=NoTurnEndCheck(),
        workspaces=FakeWorkspaces(workspace=workspace_svc, git=GitRunner(paths.bin)),
    )
    new_approvals_service = ApprovalsService(
        databases=databases,
        sessions=new_manager,
        audit_log=audit_log,
        event_log=event_log,
    )

    # Approve using the rebuilt manager directly, calling decide first without subscribing
    await new_approvals_service.decide(
        project_id, apr_id, approve=True, note="Resumed after restart"
    )

    new_sub = new_manager.subscribe(project_id, "writer", after_seq=None)
    end_events = await collect_until_turn_ended(new_sub)
    turn_ended = next(e for e in end_events if isinstance(e, TurnEnded))
    assert turn_ended.data.status == "done"

    # Tool was invoked
    assert len(write_tool.invoked) == 1

    # Earlier parts preserved
    final_view = await new_manager.get(project_id, "writer")
    final_msg = [m for m in final_view.messages if m.author_kind == "agent"][-1]
    part_types = [p.type for p in final_msg.parts]
    assert "tool_call" in part_types
    assert "approval" in part_types
    assert "tool_result" in part_types

    await new_manager.close()


@pytest.mark.asyncio
async def test_resuming_turn_no_duplicates(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    """5. No duplicates: Resuming creates no second approval record or audit record."""
    write_tool = DummyWriteTool()
    tool_reg = make_tool_reg(write_tool)
    def_dir = _write_agent_def(tmp_path)

    audit_log = SqliteAuditLog(databases, secrets=[])
    event_log = SqliteEventLog(databases)
    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store, audit_log=audit_log, event_log=event_log)

    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        extra_dirs=[def_dir],
        tools=tool_reg,
        gate=gate,
    )
    manager = env.manager
    project_id = env.project_id
    await env.settings.set_default_model("writer", f"{env.provider}:fake-tools")

    approvals_service = ApprovalsService(
        databases=databases,
        sessions=manager,
        audit_log=audit_log,
        event_log=event_log,
    )

    fake_openai.state.scripted_tool_calls = [
        {"name": "write_code_tool", "arguments": {"file_path": "nodup.py"}}
    ]

    sub = manager.subscribe(project_id, "writer", after_seq=None)
    await anext(sub)

    await manager.send(project_id, "writer", "No dups please")
    await collect_until_turn_waiting(sub)

    pending = await approvals_service.pending(project_id)
    assert len(pending) == 1
    apr_id = pending[0].id

    # Exactly 1 audit record so far: "asked"
    from mlops_agents.storage.tables import audit

    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        records_before = (await conn.execute(audit.select())).mappings().all()
    assert len(records_before) == 1
    assert records_before[0]["decision"] == "asked"

    await approvals_service.decide(project_id, apr_id, approve=True, note="ok")
    await collect_until_turn_ended(sub)

    # Exactly 1 approval record exists in store
    all_approvals = await store.list_for_step(project_id, pending[0].turn_id)
    assert len(all_approvals) == 1

    # Exactly 2 audit records: "asked" and "approved" (no second "asked" on resume)
    async with engine.connect() as conn:
        records_after = (await conn.execute(audit.select())).mappings().all()
    decisions = [r["decision"] for r in records_after]
    assert decisions.count("asked") == 1
    assert decisions.count("approved") == 1

    await manager.close()


@pytest.mark.asyncio
async def test_resuming_turn_task_mismatch(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    """6. Task mismatch check:
    If current task changed between approval decision and execution, tool call is answered with error.
    """
    write_tool = DummyWriteTool()
    tool_reg = make_tool_reg(write_tool)
    def_dir = _write_agent_def(tmp_path)

    audit_log = SqliteAuditLog(databases, secrets=[])
    event_log = SqliteEventLog(databases)
    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store, audit_log=audit_log, event_log=event_log)

    from mlops_agents.agents.catalog import AgentCatalog
    from mlops_agents.integrations.github.auth import GitHubAuth
    from mlops_agents.integrations.github.git import GitRunner
    from mlops_agents.providers.models import ModelCreate, ProviderCreate
    from mlops_agents.providers.service import ProvidersService
    from mlops_agents.workspace.service import WorkspaceService
    from tests.conftest import make_repo
    from tests.integrations.test_github_auth import FakeGh

    repo_dir = make_repo(tmp_path / "repo", {"README.md": "hello"}, git_env)
    providers_svc = ProvidersService(databases, keystore, model_http)
    catalog = AgentCatalog.load(tool_reg.names(), extra_dirs=[def_dir])
    from mlops_agents.agents.settings import AgentSettingsService

    settings_svc = AgentSettingsService(databases, providers_svc, catalog)
    http = httpx2.AsyncClient()
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    git = GitRunner(paths.bin, github.token)
    settings = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)})
    workspace_svc = WorkspaceService(settings, paths, databases, git, github, http)

    workspaces = MutableTaskWorkspaces(workspace=workspace_svc, git=git)
    # Start with task A
    workspaces.current_task_ref = TaskRef(
        id="tsk_A", title="Task A", branch="task-a", worktree=tmp_path / "repo", status="active"
    )

    manager = SessionManager(
        databases=databases,
        workspace=workspace_svc,
        catalog=catalog,
        tools=tool_reg,
        settings=settings_svc,
        providers=providers_svc,
        http=model_http,
        gate=gate,
        inbound=NoInbound(),
        turn_end=NoTurnEndCheck(),
        workspaces=workspaces,
    )

    prov = await providers_svc.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake_openai.base_url)
    )
    await providers_svc.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-tools"))
    await providers_svc.verify_model(f"{prov.name}:fake-tools")

    project = await workspace_svc.add_local(str(repo_dir))
    await settings_svc.set_default_model("writer", f"{prov.name}:fake-tools")

    approvals_service = ApprovalsService(
        databases=databases,
        sessions=manager,
        audit_log=audit_log,
        event_log=event_log,
    )

    fake_openai.state.scripted_tool_calls = [
        {"name": "write_code_tool", "arguments": {"file_path": "task_check.py"}}
    ]

    sub = manager.subscribe(project.id, "writer", after_seq=None)
    await anext(sub)

    await manager.send(project.id, "writer", "Run under task A")
    await collect_until_turn_waiting(sub)

    pending = await approvals_service.pending(project.id)
    assert len(pending) == 1
    apr_id = pending[0].id

    # Approved while task A is current
    await approvals_service.decide(project.id, apr_id, approve=True, note="Approved for task A")

    # Now change current task to task B before tool runs (simulating task switch)
    workspaces.current_task_ref = TaskRef(
        id="tsk_B", title="Task B", branch="task-b", worktree=tmp_path / "repo", status="active"
    )

    end_events = await collect_until_turn_ended(sub)
    turn_ended = next(e for e in end_events if isinstance(e, TurnEnded))
    assert turn_ended.data.status == "done"

    # Tool was NOT invoked because task mismatched
    assert len(write_tool.invoked) == 0

    # Tool message reflects the task mismatch error
    session_view = await manager.get(project.id, "writer")
    agent_msg = [m for m in session_view.messages if m.author_kind == "agent"][-1]
    res_part = next(p for p in agent_msg.parts if isinstance(p, ToolResultPart))
    assert res_part.is_error is True
    assert "task" in res_part.content.lower()

    await manager.close()
