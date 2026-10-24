from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import Resync
from mlops_agents.agents.tasks.service import TasksService
from mlops_agents.agents.tasks.worktrees import TaskFiles
from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.clock import now_iso
from mlops_agents.config import RunMode, Settings, load_settings
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog, EventRecord, EventType
from mlops_agents.ids import new_id
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import sessions
from mlops_agents.workspace.service import WorkspaceService
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


async def _setup_service(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> tuple[TasksService, WorkspaceService, FakeEventLog, str, Path]:
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
    return service, workspace, events, project.id, repo


async def _create_session(
    databases: Databases, project_id: str, session_id: str, agent: str = "coder"
) -> None:
    from sqlalchemy import insert

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


async def test_start_task_creates_worktree_db_row_and_event(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, events, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_123", agent="coder")

    task_ref = await service.start(
        project_id=project_id,
        session_id="ses_123",
        agent="coder",
        title="Implement user login",
    )

    assert isinstance(task_ref, TaskRef)
    assert task_ref.id.startswith("tsk_")
    assert task_ref.title == "Implement user login"
    assert task_ref.branch.startswith("agent/coder/implement-user-login-")
    assert task_ref.status == "active"
    assert task_ref.worktree.exists()

    # Verify event recorded
    assert len(events.records) == 1
    event = events.records[0]
    assert event["project_id"] == project_id
    assert event["type"] == "task.started"
    assert event["source"] == "coder"
    assert event["payload"]["task_id"] == task_ref.id
    assert event["payload"]["title"] == "Implement user login"


async def test_current_and_resume_task(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, _, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_coder", agent="coder")
    await _create_session(databases, project_id, "ses_reviewer", agent="reviewer")

    # Initially no current task for session
    assert await service.current(project_id, "ses_coder") is None

    # Start first task
    task1 = await service.start(
        project_id=project_id,
        session_id="ses_coder",
        agent="coder",
        title="First task",
    )

    current = await service.current(project_id, "ses_coder")
    assert current is not None
    assert current.id == task1.id

    # Start second task
    task2 = await service.start(
        project_id=project_id,
        session_id="ses_coder",
        agent="coder",
        title="Second task",
    )
    current = await service.current(project_id, "ses_coder")
    assert current is not None
    assert current.id == task2.id

    # Resume first task
    resumed = await service.resume(
        project_id=project_id,
        session_id="ses_coder",
        agent="coder",
        task_id=task1.id,
    )
    assert resumed.id == task1.id
    assert (await service.current(project_id, "ses_coder")) == resumed

    # Resume fails for unknown task id
    with pytest.raises(DomainError, match="not found"):
        await service.resume(
            project_id=project_id,
            session_id="ses_coder",
            agent="coder",
            task_id="tsk_nonexistent",
        )

    # Resume fails for another agent
    with pytest.raises(DomainError, match="belongs to agent"):
        await service.resume(
            project_id=project_id,
            session_id="ses_reviewer",
            agent="reviewer",
            task_id=task1.id,
        )


async def test_view_and_open_tasks(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, _, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_coder", agent="coder")
    await _create_session(databases, project_id, "ses_reviewer", agent="reviewer")

    task1 = await service.start(
        project_id=project_id, session_id="ses_coder", agent="coder", title="Task 1"
    )
    task2 = await service.start(
        project_id=project_id, session_id="ses_reviewer", agent="reviewer", title="Task 2"
    )

    view1 = await service.view(project_id, task1.id)
    assert view1.id == task1.id
    assert view1.title == "Task 1"
    assert view1.status == "active"
    assert view1.branch == task1.branch

    open_list = await service.open_tasks(project_id)
    open_ids = [t.id for t in open_list]
    assert task1.id in open_ids
    assert task2.id in open_ids


async def test_files_returns_task_files_or_repo_files(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, _, project_id, repo = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_1", agent="coder")

    # When task is None, returns RepoFiles on repo_root
    repo_files = await service.files(project_id, None)
    assert isinstance(repo_files, RepoFiles)
    assert repo_files.root == repo

    # When task is provided, returns TaskFiles on task.worktree
    task = await service.start(
        project_id=project_id, session_id="ses_1", agent="coder", title="Files task"
    )
    task_files = await service.files(project_id, task)
    assert isinstance(task_files, TaskFiles)
    assert task_files.root == task.worktree
