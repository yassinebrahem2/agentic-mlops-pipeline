from __future__ import annotations

import stat
from collections.abc import AsyncGenerator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
import pytest

from mlops_agents.agents.tasks.contract import CommitRefused
from mlops_agents.agents.tasks.local_merge import local_merge_preview
from mlops_agents.agents.tasks.service import TasksService
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
) -> tuple[TasksService, WorkspaceService, GitRunner, FakeEventLog, str, Path]:
    repo = make_repo(tmp_path / "repo", {"main.py": "print('initial')\n"}, git_env)
    http = httpx2.AsyncClient()
    github = GitHubAuth(databases, keystore, http, FakeGh(None))
    runner = GitRunner(paths.bin, github.token)
    await runner.run(["config", "user.name", "Jane Doe"], cwd=repo)
    await runner.run(["config", "user.email", "jane@example.com"], cwd=repo)
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
    return service, workspace, runner, events, project.id, repo


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


async def test_local_merge_preview(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, runner, _, project_id, repo = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_preview")

    task = await service.start(
        project_id=project_id,
        session_id="ses_preview",
        agent="coder",
        title="Add feature file",
    )

    (task.worktree / "feature.py").write_text("print('feature')\n", encoding="utf-8")
    await service.commit(project_id, task, "Add feature")

    # Preview directly via function
    preview = await local_merge_preview(
        runner=runner,
        repo_root=repo,
        empty_hooks=paths.empty_hooks,
        default_branch="main",
        task_branch=task.branch,
    )
    assert preview == ["feature.py"]

    # Preview via service method
    svc_preview = await service.local_merge_preview(project_id, task)
    assert svc_preview == ["feature.py"]


async def test_local_merge_clean_fast_forward_default_branch_checked_out(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, _, _, project_id, repo = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_merge_clean")

    task = await service.start(
        project_id=project_id,
        session_id="ses_merge_clean",
        agent="coder",
        title="Update main script",
    )

    (task.worktree / "main.py").write_text("print('updated by task')\n", encoding="utf-8")
    (task.worktree / "helper.py").write_text("print('helper')\n", encoding="utf-8")
    await service.commit(project_id, task, "Implement update")

    # Local merge via service
    await service.local_merge(project_id, task)

    # In user repo, main.py is updated and helper.py exists
    assert (repo / "main.py").read_text(encoding="utf-8") == "print('updated by task')\n"
    assert (repo / "helper.py").read_text(encoding="utf-8") == "print('helper')\n"

    # Temporary merge worktree is removed
    temp_worktree = paths.project_dir(project_id) / "temp_merges" / f"merge_{task.id}"
    assert not temp_worktree.exists()


async def test_local_merge_uncommitted_changes_in_way_raises_commit_refused(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, _, _, project_id, repo = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_merge_dirty")

    task = await service.start(
        project_id=project_id,
        session_id="ses_merge_dirty",
        agent="coder",
        title="Update main script dirty",
    )

    (task.worktree / "main.py").write_text("print('from task')\n", encoding="utf-8")
    await service.commit(project_id, task, "Task commit")

    # Plant an uncommitted change in the user's working tree in main.py
    (repo / "main.py").write_text("print('user uncommitted edit')\n", encoding="utf-8")

    with pytest.raises(CommitRefused):
        await service.local_merge(project_id, task)

    # User's uncommitted edit is preserved, not overwritten
    assert (repo / "main.py").read_text(encoding="utf-8") == "print('user uncommitted edit')\n"

    # Temp worktree cleaned up
    temp_worktree = paths.project_dir(project_id) / "temp_merges" / f"merge_{task.id}"
    assert not temp_worktree.exists()


async def test_local_merge_default_branch_not_checked_out(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, runner, _, project_id, repo = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_merge_not_checked_out")

    task = await service.start(
        project_id=project_id,
        session_id="ses_merge_not_checked_out",
        agent="coder",
        title="Background task",
    )

    (task.worktree / "bg.py").write_text("print('bg')\n", encoding="utf-8")
    commit_res = await service.commit(project_id, task, "Background feature")

    # Switch repo's worktree to another branch 'user-work'
    await runner.run(["checkout", "-b", "user-work"], cwd=repo)
    assert (
        await runner.run(["symbolic-ref", "--short", "HEAD"], cwd=repo)
    ).stdout.strip() == "user-work"

    # Local merge when main is not checked out in any worktree
    await service.local_merge(project_id, task)

    # user-work HEAD is untouched (still on user-work, bg.py not in working tree)
    assert not (repo / "bg.py").exists()
    assert (
        await runner.run(["symbolic-ref", "--short", "HEAD"], cwd=repo)
    ).stdout.strip() == "user-work"

    # But refs/heads/main has been updated to the merge commit!
    main_rev = (await runner.run(["rev-parse", "refs/heads/main"], cwd=repo)).stdout.strip()
    # It should have commit_res.sha as parent
    parents = (
        (await runner.run(["rev-list", "--parents", "-n", "1", main_rev], cwd=repo))
        .stdout.strip()
        .split()
    )
    assert commit_res.sha in parents


async def test_local_merge_conflicting_changes_raise_commit_refused(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, runner, _, project_id, repo = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_merge_conflict")

    task = await service.start(
        project_id=project_id,
        session_id="ses_merge_conflict",
        agent="coder",
        title="Conflict task",
    )

    # Task modifies main.py
    (task.worktree / "main.py").write_text("print('task conflicting version')\n", encoding="utf-8")
    await service.commit(project_id, task, "Task version")

    # User modifies main.py on main and commits it
    (repo / "main.py").write_text("print('user conflicting version')\n", encoding="utf-8")
    await runner.run(["commit", "-am", "User version on main"], cwd=repo)

    with pytest.raises(CommitRefused, match=r"Merge conflict in: main\.py"):
        await service.local_merge(project_id, task)

    # User repo is untouched
    assert (repo / "main.py").read_text(encoding="utf-8") == "print('user conflicting version')\n"

    # Temp worktree is cleaned up
    temp_worktree = paths.project_dir(project_id) / "temp_merges" / f"merge_{task.id}"
    assert not temp_worktree.exists()


async def test_local_merge_post_merge_hook_does_not_run(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, _, _, project_id, repo = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_hook_test")

    task = await service.start(
        project_id=project_id,
        session_id="ses_hook_test",
        agent="coder",
        title="Hook test task",
    )

    (task.worktree / "hook_test.py").write_text("print('ok')\n", encoding="utf-8")
    await service.commit(project_id, task, "Hook commit")

    # Plant a post-merge hook that fails if executed
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    post_merge = hooks_dir / "post-merge"
    post_merge.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    post_merge.chmod(post_merge.stat().st_mode | stat.S_IEXEC)

    # Merge must succeed because empty_hooks disables core.hooksPath
    await service.local_merge(project_id, task)
    assert (repo / "hook_test.py").exists()
