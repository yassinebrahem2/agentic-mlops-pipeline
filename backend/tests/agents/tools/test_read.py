from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import cast

import pytest

from mlops_agents.agents.tools.base import ToolContext
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.agents.tools.read import ReadTool
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.ctx import tool_context
from tests.agents.tools.test_tasks import create_session, setup_service
from tests.conftest import MemoryKeyStore, make_repo


async def test_reads_with_line_numbers(ctx: ToolContext) -> None:
    out = await ReadTool().invoke(ctx, {"file_path": "src/app.py"})
    assert not out.is_error
    assert out.content.splitlines()[2] == "     3\tdef train(model):"


async def test_offset_and_limit(ctx: ToolContext) -> None:
    out = await ReadTool().invoke(ctx, {"file_path": "src/app.py", "offset": 3, "limit": 2})
    assert out.content.splitlines()[:2] == [
        "     3\tdef train(model):",
        "     4\t    return model.fit()",
    ]
    assert "continues after line 4" in out.content


async def test_large_file_is_cut_with_a_note(ctx: ToolContext) -> None:
    out = await ReadTool().invoke(ctx, {"file_path": "big.txt"})
    lines = out.content.splitlines()
    assert lines[1999].endswith("line 2000")
    assert "Read again with offset=2001" in lines[-1]


async def test_binary_and_non_utf8(ctx: ToolContext) -> None:
    binary = await ReadTool().invoke(ctx, {"file_path": "data.bin"})
    assert "binary file" in binary.content
    latin = await ReadTool().invoke(ctx, {"file_path": "latin1.txt"})
    assert "caf� au lait" in latin.content


async def test_errors_are_results(ctx: ToolContext) -> None:
    assert (await ReadTool().invoke(ctx, {"file_path": "untracked.txt"})).is_error
    assert (await ReadTool().invoke(ctx, {"path": "README.md"})).is_error


async def test_whole_file_read_exceeding_budget_refused(ctx: ToolContext) -> None:
    # Set cap to 100 characters on ToolContext
    limited_ctx = ToolContext(
        project_id=ctx.project_id,
        agent=ctx.agent,
        session_id=ctx.session_id,
        turn_id=ctx.turn_id,
        files=ctx.files,
        task=ctx.task,
        tool_call_id=ctx.tool_call_id,
        tool_result_max_chars=100,
    )
    # big.txt has 2000 lines, > 100 bytes
    res = await ReadTool().invoke(limited_ctx, {"file_path": "big.txt"})
    assert res.is_error
    assert "too large to read whole" in res.content
    assert "bytes" in res.content
    assert "lines" in res.content
    assert "offset and limit" in res.content
    assert "Grep" in res.content


async def test_read_with_range_larger_than_budget_cut_with_notice(ctx: ToolContext) -> None:
    limited_ctx = ToolContext(
        project_id=ctx.project_id,
        agent=ctx.agent,
        session_id=ctx.session_id,
        turn_id=ctx.turn_id,
        files=ctx.files,
        task=ctx.task,
        tool_call_id=ctx.tool_call_id,
        tool_result_max_chars=100,
    )
    # Read with range (offset=1, limit=50) on big.txt
    res = await ReadTool().invoke(limited_ctx, {"file_path": "big.txt", "offset": 1, "limit": 50})
    assert not res.is_error
    assert "characters cut to fit context budget" in res.content


@pytest.mark.serial
async def test_oversized_single_line_file_reads_bounded(
    tmp_path: Path, git_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # 50 MB single line file without newlines
    file_bytes = b"x" * 50_000_000
    root = make_repo(tmp_path / "repo", {"huge.txt": file_bytes.decode("latin1")}, git_env)
    ctx = tool_context(RepoFiles(root, GitRunner(tmp_path / "bin")))

    class TrackingReader(io.BufferedReader):
        def readline(self, size: int | None = -1) -> bytes:
            assert size is not None and size > 0, "readline() called without a size parameter"
            return super().readline(size)

    original_open = Path.open

    def wrapped_open(
        self: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> io.BufferedReader:
        handle = original_open(self, mode, buffering, encoding, errors, newline)
        if self.name == "huge.txt" and isinstance(handle, io.BufferedReader):
            handle.close()
            return TrackingReader(io.FileIO(str(self), "rb"))
        return cast(io.BufferedReader, handle)

    monkeypatch.setattr(Path, "open", wrapped_open)

    out = await ReadTool().invoke(ctx, {"file_path": "huge.txt"})
    assert not out.is_error
    lines = out.content.splitlines()
    assert len(lines) == 1
    assert lines[0].endswith(" [line cut]")
    assert len(lines[0]) <= 2025


@pytest.mark.parametrize(
    ("text", "cut"),
    [
        ("x" * 2000, False),
        ("é" * 2000, False),
        ("€" * 2000, False),
        ("x" * 2001, True),
        ("€" * 2001, True),
    ],
    ids=["ascii-full", "2-byte-full", "3-byte-full", "ascii-cut", "3-byte-cut"],
)
async def test_line_cut_counts_characters_not_bytes(
    text: str, cut: bool, tmp_path: Path, git_env: dict[str, str]
) -> None:
    root = make_repo(tmp_path / "repo", {"long.txt": f"{text}\nnext\n"}, git_env)
    ctx = tool_context(RepoFiles(root, GitRunner(tmp_path / "bin")))
    out = await ReadTool().invoke(ctx, {"file_path": "long.txt"})
    lines = out.content.splitlines()
    assert lines[0] == f"     1\t{text[:2000]}" + (" [line cut]" if cut else "")
    assert lines[1] == "     2\tnext"


async def test_read_records_hash_only_inside_task(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_read_test"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Task 1")
    task_files = await service.files(project_id, task)

    # Context without a task -> ReadTool(tasks=service) does NOT record hash
    ctx_no_task = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=None
    )
    read_tool = ReadTool(tasks=service)
    out1 = await read_tool.invoke(ctx_no_task, {"file_path": "app.py"})
    assert not out1.is_error
    assert await service.read_hash(project_id, task, "app.py") is None

    # Context with a task -> ReadTool(tasks=service) records sha256
    ctx_with_task = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )
    out2 = await read_tool.invoke(ctx_with_task, {"file_path": "app.py"})
    assert not out2.is_error
    h1 = await service.read_hash(project_id, task, "app.py")
    expected_hash = hashlib.sha256((task.worktree / "app.py").read_bytes()).hexdigest()
    assert h1 == expected_hash


async def test_read_updates_hash_when_file_changes(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_read_hash_update"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Task 1")
    task_files = await service.files(project_id, task)
    ctx_with_task = tool_context(
        task_files, project_id=project_id, agent=agent, session_id=session_id, task=task
    )

    read_tool = ReadTool(tasks=service)
    await read_tool.invoke(ctx_with_task, {"file_path": "app.py"})
    h1 = await service.read_hash(project_id, task, "app.py")
    expected_hash1 = hashlib.sha256((task.worktree / "app.py").read_bytes()).hexdigest()
    assert h1 == expected_hash1

    # Modify file
    (task.worktree / "app.py").write_text("print('updated content')\n")
    await read_tool.invoke(ctx_with_task, {"file_path": "app.py"})
    h2 = await service.read_hash(project_id, task, "app.py")
    expected_hash2 = hashlib.sha256((task.worktree / "app.py").read_bytes()).hexdigest()
    assert h2 == expected_hash2
    assert h2 != h1
