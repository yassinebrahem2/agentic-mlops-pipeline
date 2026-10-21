from __future__ import annotations

import io
from pathlib import Path
from typing import cast

import pytest

from mlops_agents.agents.tools.base import ToolContext
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.agents.tools.read import ReadTool
from mlops_agents.integrations.github.git import GitRunner
from tests.conftest import make_repo


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


async def test_oversized_single_line_file_reads_bounded(
    tmp_path: Path, git_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # 50 MB single line file without newlines
    file_bytes = b"x" * 50_000_000
    root = make_repo(tmp_path / "repo", {"huge.txt": file_bytes.decode("latin1")}, git_env)
    ctx = ToolContext(RepoFiles(root, GitRunner(tmp_path / "bin")))

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
