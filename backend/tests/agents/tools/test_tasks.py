from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
from sqlalchemy import insert

from mlops_agents.agents.runtime.contract import Resync
from mlops_agents.agents.tasks.service import TasksService
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import RiskClass
from mlops_agents.agents.tools.tasks import ResumeTaskTool, StartTaskTool
from mlops_agents.clock import now_iso
from mlops_agents.config import RunMode, Settings, load_settings
from mlops_agents.events.contract import EventLog, EventRecord, EventType
from mlops_agents.ids import new_id
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import sessions
from mlops_agents.workspace.service import WorkspaceService
from tests.agents.ctx import tool_context
from tests.conftest import MemoryKeyStore, make_repo
from tests.integrations.test_github_auth import FakeGh


class FakeEventLog(EventLog):
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(
        self,
        project_id: str,
        *,
        source: str,
        type: EventType,
        payload: dict[str, Any],
        subject_id: str | None = None,
    ) -> EventRecord:
        record = EventRecord(
            id=new_id("evt"),
            seq=len(self.records) + 1,
            ts=now_iso(),
            source=source,
            type=type,
            subject_id=subject_id,
            payload=payload,
        )
        self.records.append(
            {
                "project_id": project_id,
                "source": source,
                "type": type,
                "payload": payload,
                "subject_id": subject_id,
                "record": record,
            }
        )
        return record

    async def subscribe(
        self, project_id: str, after_seq: int | None
    ) -> AsyncGenerator[EventRecord | Resync, None]:
        if False:
            yield

    async def history(
        self, project_id: str, *, types: Sequence[EventType], limit: int = 200
    ) -> list[EventRecord]:
        return [
            r["record"]
            for r in reversed(self.records)
            if r["project_id"] == project_id and r["type"] in types
        ][:limit]


def _settings(paths: DataPaths) -> Settings:
    base = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)})
    return replace(base, mode=RunMode.LOCAL)


async def setup_service(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> tuple[TasksService, str]:
    repo = make_repo(tmp_path / "repo", {"app.py": "print('hello')\n"}, git_env)
    http = httpx2.AsyncClient()
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    runner = GitRunner(paths.bin, github.token)
    workspace = WorkspaceService(_settings(paths), paths, databases, runner, github, http)
    project = await workspace.add_local(str(repo))
    events = FakeEventLog()
    service = TasksService(
        databases=databases,
        paths=paths,
        git=runner,
        events=events,
        workspace=workspace,
    )
    return service, project.id


async def create_session(
    databases: Databases, project_id: str, session_id: str, agent: str = "coder"
) -> None:
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            insert(sessions).values(
                id=session_id,
                agent=agent,
                summary=None,
                compacted_through=None,
                created_at=now_iso(),
            )
        )


async def test_tool_metadata(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _ = await setup_service(tmp_path, paths, databases, keystore, git_env)
    start_tool = StartTaskTool(service)
    assert start_tool.name == "StartTask"
    assert start_tool.risk == RiskClass.WRITE_CODE
    assert "workspace" in start_tool.description.lower()

    resume_tool = ResumeTaskTool(service)
    assert resume_tool.name == "ResumeTask"
    assert resume_tool.risk == RiskClass.WRITE_CODE
    assert "resume" in resume_tool.description.lower()


async def test_start_task_creates_task_and_returns_details(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_start_test"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    files = await service.files(project_id, None)
    ctx = tool_context(files, project_id=project_id, agent=agent, session_id=session_id)

    tool = StartTaskTool(service)
    out = await tool.invoke(ctx, {"title": "Build feature"})
    assert not out.is_error

    current = await service.current(project_id, session_id)
    assert current is not None
    assert current.title == "Build feature"
    assert current.status == "active"
    assert f"Task {current.id} ({current.title})" in out.content
    assert f"Branch: {current.branch}" in out.content
    assert f"Worktree: {current.worktree}" in out.content


async def test_start_task_refused_when_unpublished_changes(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_unpub_test"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    tool = StartTaskTool(service)
    files = await service.files(project_id, None)
    ctx = tool_context(files, project_id=project_id, agent=agent, session_id=session_id)

    # Start first task
    out1 = await tool.invoke(ctx, {"title": "Task 1"})
    assert not out1.is_error
    t1 = await service.current(project_id, session_id)
    assert t1 is not None

    # Dirty the worktree
    (t1.worktree / "app.py").write_text("print('dirty')\n")
    assert await service.has_unpublished_changes(project_id, t1)

    # Attempt to start a second task while t1 is dirty -> refused
    t1_files = await service.files(project_id, t1)
    ctx_t1 = tool_context(
        t1_files, project_id=project_id, agent=agent, session_id=session_id, task=t1
    )
    out2 = await tool.invoke(ctx_t1, {"title": "Task 2"})
    assert out2.is_error
    assert "unpublished changes" in out2.content

    # Clean the worktree
    (t1.worktree / "app.py").write_text("print('hello')\n")
    assert not await service.has_unpublished_changes(project_id, t1)

    # Now starting a second task succeeds
    out3 = await tool.invoke(ctx_t1, {"title": "Task 2"})
    assert not out3.is_error
    t2 = await service.current(project_id, session_id)
    assert t2 is not None
    assert t2.title == "Task 2"
    assert t2.id != t1.id


async def test_resume_task_and_refusals(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_resume_test"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    start_tool = StartTaskTool(service)
    resume_tool = ResumeTaskTool(service)
    files = await service.files(project_id, None)
    ctx = tool_context(files, project_id=project_id, agent=agent, session_id=session_id)

    # Start task 1
    out1 = await start_tool.invoke(ctx, {"title": "Task 1"})
    assert not out1.is_error
    t1 = await service.current(project_id, session_id)
    assert t1 is not None

    # Start task 2
    out2 = await start_tool.invoke(ctx, {"title": "Task 2"})
    assert not out2.is_error
    t2 = await service.current(project_id, session_id)
    assert t2 is not None
    assert t2.id != t1.id

    # Resume task 1
    out_resume = await resume_tool.invoke(ctx, {"task_id": t1.id})
    assert not out_resume.is_error
    assert f"Task {t1.id} ({t1.title})" in out_resume.content
    assert f"Branch: {t1.branch}" in out_resume.content
    assert f"Worktree: {t1.worktree}" in out_resume.content

    current = await service.current(project_id, session_id)
    assert current is not None
    assert current.id == t1.id

    # Refuse unknown task
    out_unknown = await resume_tool.invoke(ctx, {"task_id": "tsk_unknown999"})
    assert out_unknown.is_error
    assert "not found" in out_unknown.content

    # Refuse another agent's task
    other_session = "ses_other"
    await create_session(databases, project_id, other_session, agent="reviewer")
    other_task = await service.start(project_id, other_session, "reviewer", "Review PR")

    out_other = await resume_tool.invoke(ctx, {"task_id": other_task.id})
    assert out_other.is_error
    assert "belongs to agent 'reviewer', not 'coder'" in out_other.content


def test_default_registry_registration(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    # Without tasks
    reg_no_tasks = default_registry()
    assert reg_no_tasks.names() == frozenset({"Read", "Glob", "Grep"})

    # With tasks
    reg_with_tasks = default_registry(tasks=object())  # pyright: ignore[reportArgumentType]
    assert reg_with_tasks.names() == frozenset({"Read", "Glob", "Grep", "StartTask", "ResumeTask"})
