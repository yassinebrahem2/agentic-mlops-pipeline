from __future__ import annotations

import shutil
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
from sqlalchemy import insert, select

from mlops_agents.agents.tasks.lifecycle import set_review_task
from mlops_agents.agents.tasks.service import TasksService
from mlops_agents.agents.tools.base import ToolContext
from mlops_agents.agents.tools.read import ReadArgs, ReadTool
from mlops_agents.clock import now_iso
from mlops_agents.config import RunMode, Settings, load_settings
from mlops_agents.events.contract import EventLog, EventRecord, EventType
from mlops_agents.ids import new_id
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import file_claims, sessions, task_reads, tasks
from mlops_agents.workspace.service import WorkspaceService
from tests.conftest import MemoryKeyStore, make_repo, memory_project_secrets
from tests.integrations.test_github_auth import FakeGh


class FakeSandbox:
    def __init__(self) -> None:
        self.paused_calls: list[tuple[str, str]] = []
        self.remove_calls: list[tuple[str, str]] = []
        self.is_paused: bool = False

    @asynccontextmanager
    async def paused(self, project_id: str, task_id: str) -> AsyncGenerator[None, None]:
        self.paused_calls.append((project_id, task_id))
        self.is_paused = True
        try:
            yield
        finally:
            self.is_paused = False

    async def remove_task(self, project_id: str, task_id: str) -> None:
        self.remove_calls.append((project_id, task_id))


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
    ) -> AsyncGenerator[EventRecord, None]:
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
    sandbox: FakeSandbox | None = None,
) -> tuple[TasksService, WorkspaceService, GitRunner, FakeEventLog, str, Path]:
    repo = make_repo(tmp_path / "repo", {"hello.py": "print('hello')\n"}, git_env)
    http = httpx2.AsyncClient()
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    runner = GitRunner(paths.bin, github.token)
    await runner.run(["config", "user.name", "Jane Doe"], cwd=repo)
    await runner.run(["config", "user.email", "jane@example.com"], cwd=repo)
    workspace = WorkspaceService(
        _settings(paths), paths, databases, runner, github, http, memory_project_secrets(databases)
    )
    project = await workspace.add_local(str(repo))
    events = FakeEventLog()
    service = TasksService(
        databases=databases,
        paths=paths,
        git=runner,
        events=events,
        workspace=workspace,
        sandbox=sandbox,  # type: ignore[arg-type]
    )
    return service, workspace, runner, events, project.id, repo


async def _create_session(
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


async def test_end_task_merged_removes_worktree_branch_claims_reads_and_container(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    fake_sb = FakeSandbox()
    service, _, runner, events, project_id, repo = await _setup_service(
        tmp_path, paths, databases, keystore, git_env, sandbox=fake_sb
    )
    await _create_session(databases, project_id, "ses_lifecycle_1")

    task = await service.start(
        project_id=project_id,
        session_id="ses_lifecycle_1",
        agent="coder",
        title="Lifecycle task",
    )
    assert task.worktree.exists()

    # Claim a path and record a read
    await service.claim(project_id, task, ["hello.py"])
    await service.record_read(project_id, task, "hello.py", "somehash")

    # Call end with status "merged"
    await service.end(project_id, task.id, status="merged")

    # 1. Container removed
    assert (project_id, task.id) in fake_sb.remove_calls

    # 2. Worktree removed
    assert not task.worktree.exists()

    # 3. Branch deleted
    branch_check = await runner.run(
        ["rev-parse", "--verify", f"refs/heads/{task.branch}"], cwd=repo, check=False
    )
    assert branch_check.code != 0

    # 4. Claims released
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        claims_count = (
            await conn.execute(select(file_claims).where(file_claims.c.task_id == task.id))
        ).all()
        assert len(claims_count) == 0

        # 5. Reads removed
        reads_count = (
            await conn.execute(select(task_reads).where(task_reads.c.task_id == task.id))
        ).all()
        assert len(reads_count) == 0

        # 6. Task record updated
        task_row = (
            (await conn.execute(select(tasks).where(tasks.c.id == task.id))).mappings().first()
        )
        assert task_row is not None
        assert task_row["status"] == "merged"
        assert task_row["ended_at"] is not None

    # 7. Event emitted
    end_events = [e for e in events.records if e["type"] == "task.ended"]
    assert len(end_events) == 1
    assert end_events[0]["source"] == "coder"
    assert end_events[0]["subject_id"] == task.id
    assert end_events[0]["payload"] == {"task_id": task.id, "status": "merged"}


async def test_end_task_closed_keeps_branch(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, runner, _, project_id, repo = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_lifecycle_closed")

    task = await service.start(
        project_id=project_id,
        session_id="ses_lifecycle_closed",
        agent="coder",
        title="Closed task",
    )

    await service.end(project_id, task.id, status="closed")

    # Worktree removed
    assert not task.worktree.exists()

    # Branch is NOT deleted for "closed"
    branch_check = await runner.run(
        ["rev-parse", "--verify", f"refs/heads/{task.branch}"], cwd=repo, check=False
    )
    assert branch_check.code == 0


async def test_set_review_task(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, _, _, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_lifecycle_review")

    task = await service.start(
        project_id=project_id,
        session_id="ses_lifecycle_review",
        agent="coder",
        title="Review task",
    )

    view = await service.set_review(
        project_id,
        task.id,
        kind="local",
        pr_number=None,
        pr_url=None,
    )
    assert view.id == task.id
    assert view.status == "in_review"
    assert view.review_kind == "local"

    # Verify directly via set_review_task function
    view2 = await set_review_task(
        engine=await databases.project(project_id),
        project_id=project_id,
        task_id=task.id,
        kind="github",
        pr_number=42,
        pr_url="https://github.com/foo/bar/pull/42",
    )
    assert view2.status == "in_review"
    assert view2.review_kind == "github"
    assert view2.pr_number == 42
    assert view2.pr_url == "https://github.com/foo/bar/pull/42"


async def test_worktree_recreation_on_files_and_pending_note(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, _, _, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    session_id = "ses_recreate_test"
    await _create_session(databases, project_id, session_id)

    task = await service.start(
        project_id=project_id,
        session_id=session_id,
        agent="coder",
        title="Worktree recreate test",
    )

    # Commit a file so the branch has commits
    (task.worktree / "recreated.py").write_text("print('kept in branch')\n", encoding="utf-8")
    await service.commit(project_id, task, "Initial commit in task")

    # Now simulate worktree folder deletion (e.g. user manually deleted it)
    shutil.rmtree(task.worktree)
    assert not task.worktree.exists()

    # current() is a pure read: it finds the task but leaves the worktree missing
    current_ref = await service.current(project_id, session_id)
    assert current_ref is not None
    assert current_ref.id == task.id
    assert not current_ref.worktree.exists()

    # files() on the turn path recreates the worktree from the branch and records the note
    files = await service.files(project_id, current_ref)
    assert current_ref.worktree.exists()
    assert (current_ref.worktree / "recreated.py").read_text(
        encoding="utf-8"
    ) == "print('kept in branch')\n"
    current_ref = await service.current(project_id, session_id)
    assert current_ref is not None
    assert (
        current_ref.pending_note
        == "[Note: Uncommitted changes were lost because the task worktree was recreated.]"
    )

    # Tool execution (ReadTool) consumes the pending note
    ctx = ToolContext(
        project_id=project_id,
        agent="coder",
        session_id=session_id,
        turn_id="trn_1",
        files=files,
        task=current_ref,
    )
    read_tool = ReadTool(tasks=service)
    output = await read_tool.run(ctx, ReadArgs(file_path="recreated.py"))
    assert (
        "[Note: Uncommitted changes were lost because the task worktree was recreated.]"
        in output.content
    )

    # Second tool call does NOT have the note again
    output2 = await read_tool.run(ctx, ReadArgs(file_path="recreated.py"))
    assert (
        "[Note: Uncommitted changes were lost because the task worktree was recreated.]"
        not in output2.content
    )
