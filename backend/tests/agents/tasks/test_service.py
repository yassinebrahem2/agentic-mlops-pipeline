from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from mlops_agents.agents.tasks.worktrees import TaskFiles
from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.errors import DomainError
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import tasks
from tests.agents.tasks.helpers import create_session as _create_session
from tests.agents.tasks.helpers import setup_service as _setup_service
from tests.conftest import MemoryKeyStore


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
    assert event["subject_id"] == task_ref.id
    assert event["payload"] == {
        "task_id": task_ref.id,
        "title": "Implement user login",
        "branch": task_ref.branch,
    }


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

    # If task1's session_id was changed in DB, ses_coder's current() ignores the cache
    from sqlalchemy import update

    async with (await databases.project(project_id)).begin() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == task1.id).values(session_id="ses_reviewer")
        )
    current_coder = await service.current(project_id, "ses_coder")
    assert current_coder is None or current_coder.id != task1.id

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


async def test_resume_cleans_leftover_temp_files(
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
    task = await service.start(
        project_id=project_id,
        session_id="ses_coder",
        agent="coder",
        title="Temp cleanup test",
    )

    from mlops_agents.agents.temp_files import make_temp_name

    generated_tmp = task.worktree / make_temp_name("file.txt")
    generated_tmp.write_text("temp", encoding="utf-8")
    user_file = task.worktree / ".tmp_notes"
    user_file.write_text("user notes", encoding="utf-8")
    assert generated_tmp.exists()
    assert user_file.exists()

    await service.resume(project_id, "ses_coder", "coder", task.id)
    assert not generated_tmp.exists()
    assert user_file.exists()


async def test_task_calls_are_serialized_per_task(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_coder", agent="coder")
    task = await service.start(
        project_id=project_id,
        session_id="ses_coder",
        agent="coder",
        title="Serialization test",
    )

    (task.worktree / "work.py").write_text("x = 1\n", encoding="utf-8")

    from mlops_agents.agents.tasks import commit as commit_module

    orig_task_git = commit_module.task_git
    in_add = asyncio.Event()
    release_add = asyncio.Event()

    async def blocking_task_git(*args: Any, **kwargs: Any) -> Any:
        git_args_raw = kwargs.get("args")
        if isinstance(git_args_raw, Sequence) and "add" in git_args_raw:
            in_add.set()
            await release_add.wait()
        return await orig_task_git(*args, **kwargs)

    monkeypatch.setattr(commit_module, "task_git", blocking_task_git)

    commit_fut = asyncio.create_task(service.commit(project_id, task, "feat: blocked add"))
    await in_add.wait()

    diff_fut = asyncio.create_task(service.staged_diff(project_id, task))
    await asyncio.sleep(0.05)
    assert not diff_fut.done()

    release_add.set()
    commit_res = await commit_fut
    assert commit_res.changed == ("work.py",)

    diff_res = await diff_fut
    assert diff_res == ""


async def test_end_and_discard_wait_for_running_commit(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, project_id, _ = await _setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await _create_session(databases, project_id, "ses_coder", agent="coder")
    task = await service.start(
        project_id=project_id,
        session_id="ses_coder",
        agent="coder",
        title="End serialization test",
    )

    (task.worktree / "work.py").write_text("x = 1\n", encoding="utf-8")

    from mlops_agents.agents.tasks import commit as commit_module

    orig_task_git = commit_module.task_git
    in_add = asyncio.Event()
    release_add = asyncio.Event()

    async def blocking_task_git(*args: Any, **kwargs: Any) -> Any:
        git_args_raw = kwargs.get("args")
        if isinstance(git_args_raw, Sequence) and "add" in git_args_raw:
            in_add.set()
            await release_add.wait()
        return await orig_task_git(*args, **kwargs)

    monkeypatch.setattr(commit_module, "task_git", blocking_task_git)

    commit_fut = asyncio.create_task(service.commit(project_id, task, "feat: blocked add"))
    await in_add.wait()

    end_fut = asyncio.create_task(service.end(project_id, task.id, "closed"))
    await asyncio.sleep(0.05)
    assert not end_fut.done()

    release_add.set()
    commit_res = await commit_fut
    assert commit_res.changed == ("work.py",)

    await end_fut
    view = await service.view(project_id, task.id)
    assert view.status == "closed"
