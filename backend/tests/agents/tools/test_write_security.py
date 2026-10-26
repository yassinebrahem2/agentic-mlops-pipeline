from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest

from mlops_agents.agents.tools.base import ToolFailure
from mlops_agents.agents.tools.write import WriteArgs, WriteTool
from mlops_agents.paths import DataPaths
from mlops_agents.sandbox.contract import Sandbox
from mlops_agents.storage.databases import Databases
from tests.agents.ctx import tool_context
from tests.agents.tools.test_tasks import create_session, setup_service
from tests.conftest import MemoryKeyStore


class FakeSandbox:
    def __init__(self) -> None:
        self.paused_calls: list[tuple[str, str]] = []

    @asynccontextmanager
    async def paused(self, project_id: str, task_id: str) -> AsyncGenerator[None, None]:
        self.paused_calls.append((project_id, task_id))
        yield


async def test_write_refuses_denylisted_names(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_denylist"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Denylist test")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    tool = WriteTool(service)
    for bad_name in [".env", ".env.local", "secret.pem", "id_rsa"]:
        res = await tool.invoke(ctx, {"file_path": bad_name, "content": "SECRET=123\n"})
        assert res.is_error
        assert "secret" in res.content.lower()


async def test_write_refuses_symlink_escape(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_symlink"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Symlink test")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")

    link = task.worktree / "escape_link.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks not supported in this environment")

    tool = WriteTool(service)
    res = await tool.invoke(ctx, {"file_path": "escape_link.py", "content": "pwned\n"})
    assert res.is_error
    assert (
        "outside" in res.content.lower()
        or "symlink" in res.content.lower()
        or "worktree" in res.content.lower()
    )


async def test_write_refuses_claimed_path(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    await create_session(databases, project_id, "ses_coder", agent="coder")
    await create_session(databases, project_id, "ses_reviewer", agent="reviewer")

    task1 = await service.start(project_id, "ses_coder", "coder", "Task 1")
    task2 = await service.start(project_id, "ses_reviewer", "reviewer", "Task 2")

    # Task 1 claims shared.py
    await service.claim(project_id, task1, ["shared.py"])

    # Task 2 attempts to Write to shared.py
    task2_files = await service.files(project_id, task2)
    ctx2 = tool_context(
        task2_files, project_id=project_id, agent="reviewer", session_id="ses_reviewer", task=task2
    )

    tool = WriteTool(service)
    res = await tool.invoke(ctx2, {"file_path": "shared.py", "content": "print(1)\n"})
    assert res.is_error
    assert "coder" in res.content
    assert task1.id in res.content
    assert "SendMessage" in res.content


async def test_write_path_traversal_refused(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_traversal"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Traversal test")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    tool = WriteTool(service)
    res = await tool.invoke(ctx, {"file_path": "../outside.txt", "content": "bad\n"})
    assert res.is_error
    assert "outside the worktree" in res.content


async def test_write_rechecks_at_run_time(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_recheck"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Recheck test")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    from mlops_agents.agents.tools.read import ReadTool

    read_tool = ReadTool(service)
    await read_tool.invoke(ctx, {"file_path": "app.py"})

    tool = WriteTool(service)
    args = WriteArgs(file_path="app.py", content="print('new')\n")
    preview = await tool.preview(ctx, args)
    assert preview.kind == "diff"

    # Now externally change app.py before run() is called
    (task.worktree / "app.py").write_text("print('external change')\n", encoding="utf-8")

    with pytest.raises(ToolFailure, match=r"app\.py changed since you read it\."):
        await tool.run(ctx, args)


async def test_write_planted_symlink_after_validation_refused(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_planted_symlink"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Planted symlink test")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    outside = tmp_path / "host_file.txt"
    outside.write_text("critical host data\n", encoding="utf-8")

    fake_sandbox = FakeSandbox()
    tool = WriteTool(service, sandbox=cast(Sandbox, fake_sandbox))
    args = WriteArgs(file_path="sub/target.py", content="evil payload\n")

    # Step 1: Preview succeeds before symlink exists
    preview = await tool.preview(ctx, args)
    assert preview.kind == "diff"

    # Step 2: Plant symlink at sub/target.py pointing to host file outside worktree
    target = task.worktree / "sub" / "target.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks not supported in this environment")

    # Step 3: Run tool - must refuse and not write through symlink
    with pytest.raises(
        ToolFailure,
        match=r"(outside the worktree|Cannot write directly to symlink|File escaped worktree)",
    ):
        await tool.run(ctx, args)

    # Step 4: Sandbox was paused, and host file was not overwritten
    assert fake_sandbox.paused_calls == [(project_id, task.id)]
    assert outside.read_text(encoding="utf-8") == "critical host data\n"


async def test_write_refuses_symlinked_parent_directory(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_symlink_dir"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Symlink dir test")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    outside_file = outside_dir / "secret.txt"
    outside_file.write_text("secret\n", encoding="utf-8")

    symlink_dir = task.worktree / "symlink_dir"
    try:
        symlink_dir.symlink_to(outside_dir, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks not supported in this environment")

    tool = WriteTool(service)
    res = await tool.invoke(
        ctx, {"file_path": "symlink_dir/secret.txt", "content": "overwritten\n"}
    )
    assert res.is_error
    assert outside_file.read_text(encoding="utf-8") == "secret\n"


async def test_write_refuses_windows_secret_names(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_win_sec"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Secret test")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    tool = WriteTool(service)
    res1 = await tool.invoke(ctx, {"file_path": ".env.", "content": "KEY=val\n"})
    assert res1.is_error
    assert "secret file" in res1.content

    res2 = await tool.invoke(ctx, {"file_path": ".env ", "content": "KEY=val\n"})
    assert res2.is_error
    assert "secret file" in res2.content
