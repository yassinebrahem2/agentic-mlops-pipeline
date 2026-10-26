from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest

from mlops_agents.agents.tools.base import ToolFailure
from mlops_agents.agents.tools.edit import EditArgs, EditTool
from mlops_agents.agents.tools.read import ReadTool
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


async def test_edit_refuses_claimed_path(
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

    # Task 1 claims app.py
    await service.claim(project_id, task1, ["app.py"])

    task2_files = await service.files(project_id, task2)
    ctx2 = tool_context(
        task2_files, project_id=project_id, agent="reviewer", session_id="ses_reviewer", task=task2
    )

    # Read app.py in task2
    read_tool = ReadTool(service)
    await read_tool.invoke(ctx2, {"file_path": "app.py"})

    tool = EditTool(service)
    res = await tool.invoke(
        ctx2, {"file_path": "app.py", "old_string": "hello", "new_string": "world"}
    )
    assert res.is_error
    assert "coder" in res.content
    assert task1.id in res.content
    assert "SendMessage" in res.content


async def test_edit_path_traversal_refused(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_traversal_edit"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Traversal test")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    tool = EditTool(service)
    res = await tool.invoke(
        ctx, {"file_path": "../outside.txt", "old_string": "a", "new_string": "b"}
    )
    assert res.is_error
    assert "outside the worktree" in res.content


async def test_edit_denylisted_name_refused(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_denylist_edit"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Denylist test")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    tool = EditTool(service)
    res = await tool.invoke(ctx, {"file_path": ".env", "old_string": "a", "new_string": "b"})
    assert res.is_error
    assert "secret" in res.content.lower()


async def test_edit_rechecks_at_run_time(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_recheck_edit"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Recheck test")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    read_tool = ReadTool(service)
    await read_tool.invoke(ctx, {"file_path": "app.py"})

    tool = EditTool(service)
    args = EditArgs(file_path="app.py", old_string="hello", new_string="world")
    preview = await tool.preview(ctx, args)
    assert preview.kind == "diff"

    # Modify app.py before run()
    (task.worktree / "app.py").write_text("print('external')\n", encoding="utf-8")

    with pytest.raises(ToolFailure, match=r"app\.py changed since you read it\."):
        await tool.run(ctx, args)


async def test_edit_planted_symlink_after_validation_refused(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_planted_edit"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Planted edit test")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    outside = tmp_path / "host_edit.txt"
    outside.write_text("precious host data\n", encoding="utf-8")

    (task.worktree / "target.txt").write_text("hello world\n", encoding="utf-8")

    read_tool = ReadTool(service)
    await read_tool.invoke(ctx, {"file_path": "target.txt"})

    fake_sandbox = FakeSandbox()
    tool = EditTool(service, sandbox=cast(Sandbox, fake_sandbox))
    args = EditArgs(file_path="target.txt", old_string="hello", new_string="goodbye")

    preview = await tool.preview(ctx, args)
    assert preview.kind == "diff"

    # Now plant symlink at target.txt pointing to outside file
    (task.worktree / "target.txt").unlink()
    try:
        (task.worktree / "target.txt").symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks not supported in this environment")

    with pytest.raises(
        ToolFailure,
        match=r"(outside the worktree|Cannot write directly to symlink|File escaped worktree)",
    ):
        await tool.run(ctx, args)

    assert fake_sandbox.paused_calls == [(project_id, task.id)]
    assert outside.read_text(encoding="utf-8") == "precious host data\n"
