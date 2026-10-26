from __future__ import annotations

from pathlib import Path

from mlops_agents.agents.tools.base import RiskClass
from mlops_agents.agents.tools.edit import EditArgs, EditTool
from mlops_agents.agents.tools.read import ReadTool
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.ctx import tool_context
from tests.agents.tools.test_tasks import create_session, setup_service
from tests.conftest import MemoryKeyStore


async def test_edit_tool_metadata(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _ = await setup_service(tmp_path, paths, databases, keystore, git_env)
    tool = EditTool(service)
    assert tool.name == "Edit"
    assert tool.risk == RiskClass.WRITE_CODE
    assert tool.args_model == EditArgs


async def test_edit_requires_current_task(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_no_task_edit"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)
    task_files = await service.files(project_id, None)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=None
    )

    tool = EditTool(service)
    res = await tool.invoke(
        ctx, {"file_path": "app.py", "old_string": "hello", "new_string": "world"}
    )
    assert res.is_error
    assert "Start a task first with StartTask." in res.content


async def test_edit_nonexistent_file_fails(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_nonexistent"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Edit task")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    tool = EditTool(service)
    res = await tool.invoke(
        ctx, {"file_path": "missing.py", "old_string": "hello", "new_string": "world"}
    )
    assert res.is_error
    assert "missing.py does not exist." in res.content


async def test_edit_requires_read_first(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_edit_read"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Edit task")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    tool = EditTool(service)
    res = await tool.invoke(
        ctx, {"file_path": "app.py", "old_string": "hello", "new_string": "world"}
    )
    assert res.is_error
    assert "Read app.py first." in res.content


async def test_edit_successful_replacement_and_preview(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_edit_success"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Edit task")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    # Read app.py first
    read_tool = ReadTool(service)
    await read_tool.invoke(ctx, {"file_path": "app.py"})

    tool = EditTool(service)
    args = EditArgs(file_path="app.py", old_string="hello", new_string="universe")

    preview = await tool.preview(ctx, args)
    assert preview.kind == "diff"
    assert "Edit app.py" in preview.title
    assert "-print('hello')" in preview.body
    assert "+print('universe')" in preview.body

    res = await tool.invoke(ctx, args.model_dump())
    assert not res.is_error
    assert preview.body == res.content

    content = (task.worktree / "app.py").read_text(encoding="utf-8")
    assert content == "print('universe')\n"


async def test_edit_missing_old_string(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_edit_missing"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Edit task")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    read_tool = ReadTool(service)
    await read_tool.invoke(ctx, {"file_path": "app.py"})

    tool = EditTool(service)
    res = await tool.invoke(
        ctx, {"file_path": "app.py", "old_string": "goodbye", "new_string": "universe"}
    )
    assert res.is_error
    assert "old_string not found in app.py." in res.content


async def test_edit_duplicate_old_string_without_and_with_replace_all(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_edit_duplicates"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Edit task")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    # Put duplicate occurrences in app.py
    (task.worktree / "app.py").write_text("item = 1\nitem = 2\n", encoding="utf-8")

    read_tool = ReadTool(service)
    await read_tool.invoke(ctx, {"file_path": "app.py"})

    tool = EditTool(service)
    # Without replace_all -> fails
    res1 = await tool.invoke(
        ctx,
        {
            "file_path": "app.py",
            "old_string": "item",
            "new_string": "val",
            "replace_all": False,
        },
    )
    assert res1.is_error
    assert "old_string appears 2 times in app.py." in res1.content
    assert "Use replace_all=True" in res1.content

    # With replace_all -> succeeds
    res2 = await tool.invoke(
        ctx,
        {
            "file_path": "app.py",
            "old_string": "item",
            "new_string": "val",
            "replace_all": True,
        },
    )
    assert not res2.is_error
    assert (task.worktree / "app.py").read_text(encoding="utf-8") == "val = 1\nval = 2\n"


async def test_edit_refused_if_file_changed_after_read(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_edit_changed"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Edit task")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    read_tool = ReadTool(service)
    await read_tool.invoke(ctx, {"file_path": "app.py"})

    # Modify externally
    (task.worktree / "app.py").write_text("print('external')\n", encoding="utf-8")

    tool = EditTool(service)
    res = await tool.invoke(
        ctx, {"file_path": "app.py", "old_string": "hello", "new_string": "world"}
    )
    assert res.is_error
    assert "app.py changed since you read it." in res.content


async def test_edit_preserves_crlf(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_edit_crlf"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Edit task")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    (task.worktree / "crlf.txt").write_bytes(b"first line\r\nsecond line\r\n")

    read_tool = ReadTool(service)
    await read_tool.invoke(ctx, {"file_path": "crlf.txt"})

    tool = EditTool(service)
    res = await tool.invoke(
        ctx,
        {
            "file_path": "crlf.txt",
            "old_string": "first line",
            "new_string": "1st line",
        },
    )
    assert not res.is_error
    assert (task.worktree / "crlf.txt").read_bytes() == b"1st line\r\nsecond line\r\n"


async def test_edit_empty_old_string_is_tool_error(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_empty_old"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Empty old test")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    read_tool = ReadTool(service)
    await read_tool.invoke(ctx, {"file_path": "app.py"})

    tool = EditTool(service)
    res = await tool.invoke(ctx, {"file_path": "app.py", "old_string": "", "new_string": "foo"})
    assert res.is_error
    assert "old_string cannot be empty" in res.content


async def test_edit_non_utf8_file_is_tool_error(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_non_utf8"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Non utf8 test")
    task_files = await service.files(project_id, task)
    ctx = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    # Write non-UTF-8 bytes
    (task.worktree / "binary.bin").write_bytes(b"\x80\x81\xff\xfe non utf8 data")

    read_tool = ReadTool(service)
    await read_tool.invoke(ctx, {"file_path": "binary.bin"})

    tool = EditTool(service)
    res = await tool.invoke(
        ctx, {"file_path": "binary.bin", "old_string": "data", "new_string": "text"}
    )
    assert res.is_error
    assert "not a valid UTF-8 text file" in res.content
