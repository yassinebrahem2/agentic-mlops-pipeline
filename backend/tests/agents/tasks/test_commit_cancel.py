from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from mlops_agents.agents.tasks import commit as commit_module
from mlops_agents.agents.tasks import service as service_module
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import file_claims
from tests.agents.tasks.test_commit import cmd, create_session, setup_service
from tests.conftest import MemoryKeyStore


class FakeSandbox:
    def __init__(self) -> None:
        self.paused_calls: list[tuple[str, str]] = []
        self.is_paused: bool = False

    @asynccontextmanager
    async def paused(self, project_id: str, task_id: str) -> AsyncGenerator[None, None]:
        self.paused_calls.append((project_id, task_id))
        self.is_paused = True
        try:
            yield
        finally:
            self.is_paused = False


async def test_commit_cancellation_cleans_up_claims_and_unstages(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, project_id, _ = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Cancel test")

    await service.claim(project_id, task, ["earlier.py"])

    (task.worktree / "earlier.py").write_text("# edited\n", encoding="utf-8")
    (task.worktree / "new_file.py").write_text("# new\n", encoding="utf-8")

    orig_task_git = commit_module.task_git

    async def mock_task_git(*args: Any, **kwargs: Any) -> Any:
        git_args_raw = kwargs.get("args")
        if isinstance(git_args_raw, Sequence) and "commit" in git_args_raw:
            raise asyncio.CancelledError()
        return await orig_task_git(*args, **kwargs)

    monkeypatch.setattr(commit_module, "task_git", mock_task_git)

    with pytest.raises(asyncio.CancelledError):
        await service.commit(project_id, task, "feat: cancel commit")

    # Check database: earlier.py still claimed, new_file.py released
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        claims = set(
            (await conn.execute(select(file_claims.c.path).where(file_claims.c.task_id == task.id)))
            .scalars()
            .all()
        )
    assert "earlier.py" in claims
    assert "new_file.py" not in claims

    # Check git: nothing is staged
    diff = cmd(["git", "diff", "--cached", "--name-only"], cwd=task.worktree)
    assert diff.stdout.strip() == ""


async def test_commit_cancellation_runner_blocks_inside_commit(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, project_id, _ = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Cancel blocked commit test")

    (task.worktree / "work.py").write_text("a = 1\n", encoding="utf-8")

    orig_task_git = commit_module.task_git
    in_commit = asyncio.Event()
    release_commit = asyncio.Event()

    async def blocking_task_git(*args: Any, **kwargs: Any) -> Any:
        git_args_raw = kwargs.get("args")
        if isinstance(git_args_raw, Sequence) and "commit" in git_args_raw:
            in_commit.set()
            await release_commit.wait()
        return await orig_task_git(*args, **kwargs)

    monkeypatch.setattr(commit_module, "task_git", blocking_task_git)

    caller_task = asyncio.create_task(service.commit(project_id, task, "feat: blocked commit"))

    # Wait until git commit is executing/blocked
    await in_commit.wait()

    # Cancel the caller while runner is blocked inside git commit
    caller_task.cancel()

    # Release git commit so it finishes successfully
    release_commit.set()

    # The caller receives CancelledError
    with pytest.raises(asyncio.CancelledError):
        await caller_task

    # BUT: Because git commit finished successfully, the commit landed!
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        claims = set(
            (await conn.execute(select(file_claims.c.path).where(file_claims.c.task_id == task.id)))
            .scalars()
            .all()
        )
    assert "work.py" in claims

    # Check git log: the commit exists on HEAD!
    log_res = cmd(["git", "log", "-1", "--format=%s"], cwd=task.worktree)
    assert log_res.stdout.strip() == "feat: blocked commit"


async def test_commit_cancellation_runner_blocks_inside_add(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, project_id, _ = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Cancel blocked add test")

    (task.worktree / "work.py").write_text("a = 1\n", encoding="utf-8")

    orig_task_git = commit_module.task_git
    in_add = asyncio.Event()
    release_add = asyncio.Event()
    git_calls: list[str] = []

    async def blocking_task_git(*args: Any, **kwargs: Any) -> Any:
        git_args_raw = kwargs.get("args")
        if isinstance(git_args_raw, Sequence):
            if "add" in git_args_raw:
                git_calls.append("add")
                in_add.set()
                await release_add.wait()
            elif "reset" in git_args_raw:
                git_calls.append("reset")
        return await orig_task_git(*args, **kwargs)

    monkeypatch.setattr(commit_module, "task_git", blocking_task_git)

    caller_task = asyncio.create_task(service.commit(project_id, task, "feat: blocked add"))

    # Wait until git add is executing
    await in_add.wait()

    # Cancel the caller while runner is blocked inside git add
    caller_task.cancel()

    # Release git add so it finishes
    release_add.set()

    # The caller receives CancelledError
    with pytest.raises(asyncio.CancelledError):
        await caller_task

    # Git reset must have been called during cleanup AFTER git add finished (no race)
    assert "add" in git_calls
    assert "reset" in git_calls
    assert git_calls.index("reset") > git_calls.index("add")

    # Nothing should be staged
    diff = cmd(["git", "diff", "--cached", "--name-only"], cwd=task.worktree)
    assert diff.stdout.strip() == ""

    # Claims must be released
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        claims = (
            await conn.execute(
                select(file_claims).where(
                    file_claims.c.task_id == task.id, file_claims.c.path == "work.py"
                )
            )
        ).all()
        assert len(claims) == 0


async def test_commit_and_staged_diff_pause_and_unpause_on_raise(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sandbox = FakeSandbox()
    service, _, _, project_id, _ = await setup_service(
        tmp_path, paths, databases, keystore, git_env, sandbox=fake_sandbox
    )

    await create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Pause test")

    # 1. staged_diff enters and exits paused
    (task.worktree / "file.py").write_text("x = 1\n", encoding="utf-8")
    assert not fake_sandbox.is_paused
    diff = await service.staged_diff(project_id, task)
    assert diff
    assert not fake_sandbox.is_paused
    assert (project_id, task.id) in fake_sandbox.paused_calls

    # 2. staged_diff exits paused even when raising
    async def failing_diff(*args: Any, **kwargs: Any) -> Any:
        assert fake_sandbox.is_paused
        raise RuntimeError("diff crash")

    monkeypatch.setattr(service_module, "staged_diff_task", failing_diff)
    with pytest.raises(RuntimeError, match="diff crash"):
        await service.staged_diff(project_id, task)
    assert not fake_sandbox.is_paused

    # 3. commit enters and exits paused
    monkeypatch.undo()
    assert not fake_sandbox.is_paused
    await service.commit(project_id, task, "feat: commit pause test")
    assert not fake_sandbox.is_paused

    # 4. commit exits paused even when raising
    (task.worktree / "file2.py").write_text("y = 2\n", encoding="utf-8")

    async def failing_commit(*args: Any, **kwargs: Any) -> Any:
        assert fake_sandbox.is_paused
        raise RuntimeError("commit crash")

    monkeypatch.setattr(service_module, "commit_task", failing_commit)
    with pytest.raises(RuntimeError, match="commit crash"):
        await service.commit(project_id, task, "feat: will fail")
    assert not fake_sandbox.is_paused
