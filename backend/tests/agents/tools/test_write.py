from __future__ import annotations

from pathlib import Path

import pytest

from mlops_agents.agents.tools.base import RiskClass, ToolFailure
from mlops_agents.agents.tools.read import ReadTool
from mlops_agents.agents.tools.write import WriteArgs, WriteTool
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.ctx import tool_context
from tests.agents.tools.test_tasks import create_session, setup_service
from tests.conftest import MemoryKeyStore


async def test_write_tool_metadata(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _ = await setup_service(tmp_path, paths, databases, keystore, git_env)
    tool = WriteTool(service)
    assert tool.name == "Write"
    assert tool.risk == RiskClass.WRITE_CODE
    assert tool.args_model == WriteArgs


async def test_write_requires_current_task(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_no_task"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)
    task_files = await service.files(project_id, None)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=None
    )

    tool = WriteTool(service)
    res = await tool.invoke(ctx, {"file_path": "new.py", "content": "print('hi')\n"})
    assert res.is_error
    assert "Start a task first with StartTask." in res.content


async def test_write_new_file_diff_preview_and_claims(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_write_new"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Write task")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    tool = WriteTool(service)
    args = WriteArgs(file_path="src/new_mod.py", content="def add(a, b):\n    return a + b\n")

    preview = await tool.preview(ctx, args)
    assert preview.kind == "diff"
    assert "Write src/new_mod.py" in preview.title
    assert "+++ b/src/new_mod.py" in preview.body
    assert "+def add(a, b):" in preview.body

    res = await tool.invoke(ctx, {"file_path": args.file_path, "content": args.content})
    assert not res.is_error
    assert preview.body == res.content

    # Target exists on disk
    target = task.worktree / "src/new_mod.py"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == args.content


async def test_write_overwrite_requires_read_first(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_overwrite_read"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Overwrite task")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    tool = WriteTool(service)
    # app.py exists from make_repo, but hasn't been read in this task
    res = await tool.invoke(ctx, {"file_path": "app.py", "content": "print('overwritten')\n"})
    assert res.is_error
    assert "Read app.py first." in res.content

    # Now read app.py
    read_tool = ReadTool(service)
    read_res = await read_tool.invoke(ctx, {"file_path": "app.py"})
    assert not read_res.is_error

    # Now write succeeds
    res2 = await tool.invoke(ctx, {"file_path": "app.py", "content": "print('overwritten')\n"})
    assert not res2.is_error
    assert (task.worktree / "app.py").read_text(encoding="utf-8") == "print('overwritten')\n"


async def test_write_refused_if_file_changed_after_read(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_changed_after_read"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Change test")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    # Read app.py
    read_tool = ReadTool(service)
    await read_tool.invoke(ctx, {"file_path": "app.py"})

    # Externally modify app.py
    (task.worktree / "app.py").write_text("print('external modification')\n", encoding="utf-8")

    tool = WriteTool(service)
    res = await tool.invoke(ctx, {"file_path": "app.py", "content": "print('overwritten')\n"})
    assert res.is_error
    assert "app.py changed since you read it." in res.content


async def test_write_preserves_crlf_line_endings(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_crlf"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "CRLF test")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    # Setup CRLF file
    crlf_path = task.worktree / "crlf.txt"
    crlf_path.write_bytes(b"line1\r\nline2\r\n")

    read_tool = ReadTool(service)
    await read_tool.invoke(ctx, {"file_path": "crlf.txt"})

    tool = WriteTool(service)
    # Give LF content
    res = await tool.invoke(ctx, {"file_path": "crlf.txt", "content": "new1\nnew2\n"})
    assert not res.is_error
    # Written file must have CRLF
    data = crlf_path.read_bytes()
    assert data == b"new1\r\nnew2\r\n"


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


async def test_write_capped_diff(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_diff_cap"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Cap test")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    tool = WriteTool(service)
    huge_content = "line of code\n" * 3000
    res = await tool.invoke(ctx, {"file_path": "huge.txt", "content": huge_content})
    assert not res.is_error
    assert len(res.content) < len(huge_content)
    assert "diff cut" in res.content


async def test_registry_has_write_and_edit(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    from mlops_agents.agents.tools import default_registry

    service, _ = await setup_service(tmp_path, paths, databases, keystore, git_env)
    registry = default_registry(tasks=service)
    assert "Write" in registry.names()
    assert "Edit" in registry.names()
