from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.runtime.budget import cut_text
from mlops_agents.agents.tools.base import (
    AgentTool,
    RiskClass,
    ToolContext,
    ToolFailure,
    ToolOutput,
)
from mlops_agents.agents.tools.file_ops import pop_pending_note

if TYPE_CHECKING:
    from mlops_agents.agents.tasks.contract import Tasks

MAX_LINES = 2000
MAX_LINE_CHARS = 2000
MAX_CHARS = 100_000
BINARY_SNIFF = 8192
# UTF-8 spends at most 4 bytes on a character, so this many bytes always hold a full line of
# MAX_LINE_CHARS characters.
MAX_LINE_BYTES = 4 * MAX_LINE_CHARS
CHUNK_SIZE = 65536


class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_path: str = Field(
        description="Path of the file, relative to the repository root. Absolute paths inside the repository also work."
    )
    offset: int | None = Field(
        default=None,
        ge=1,
        description="Line number to start from (1-based). Use it for large files.",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=MAX_LINES,
        description=f"Number of lines to read. Defaults to {MAX_LINES}.",
    )


def _count_lines_and_check_binary(path: Path) -> tuple[int, bool]:
    """(line_count, is_binary). Fast check that avoids loading whole file into memory."""
    lines = 0
    ends_with_newline = True
    has_bytes = False
    with path.open("rb") as handle:
        first = handle.read(BINARY_SNIFF)
        if b"\0" in first:
            return 0, True
        if first:
            has_bytes = True
            lines += first.count(b"\n")
            ends_with_newline = first.endswith(b"\n")
        while chunk := handle.read(CHUNK_SIZE):
            has_bytes = True
            lines += chunk.count(b"\n")
            ends_with_newline = chunk.endswith(b"\n")
    if has_bytes and not ends_with_newline:
        lines += 1
    return lines, False


def _read_lines(
    path: Path, offset: int, limit: int, max_chars: int = MAX_CHARS
) -> tuple[list[str], bool, bool]:
    """(numbered lines, is_binary, more_lines_follow). Stops early; never loads the whole file."""
    with path.open("rb") as handle:
        if b"\0" in handle.read(BINARY_SNIFF):
            return [], True, False
        handle.seek(0)
        out: list[str] = []
        chars = 0
        number = 0
        while True:
            raw = handle.readline(MAX_LINE_BYTES + 1)
            if not raw:
                break
            number += 1
            if len(raw) > MAX_LINE_BYTES and not raw.endswith(b"\n"):
                while True:
                    chunk = handle.readline(CHUNK_SIZE)
                    if not chunk or chunk.endswith(b"\n"):
                        break
            if number < offset:
                continue
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + " [line cut]"
            out.append(f"{number:>6}\t{line}")
            chars += len(line)
            if len(out) >= limit or chars >= max_chars:
                return out, False, True
        return out, False, False


def _compute_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


class ReadTool(AgentTool[ReadArgs]):
    name: ClassVar[str] = "Read"
    description: ClassVar[str] = (
        "Read a text file committed to the repository. Returns lines prefixed with their line "
        "numbers. Use offset and limit to read large files in parts."
    )
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = ReadArgs

    def __init__(self, tasks: Tasks | None = None) -> None:
        self._tasks = tasks

    async def run(self, ctx: ToolContext, args: ReadArgs) -> ToolOutput:
        rel = await ctx.files.resolve_file(args.file_path)
        offset = args.offset or 1
        full_path = ctx.files.root / rel

        cap = (
            min(ctx.tool_result_max_chars, MAX_CHARS)
            if ctx.tool_result_max_chars is not None
            else MAX_CHARS
        )

        note = (
            pop_pending_note(self._tasks, ctx.project_id, ctx.task.id)
            if ctx.task is not None
            else None
        )

        def _out(content: str) -> ToolOutput:
            if note:
                content = f"{content}\n\n{note}"
            return ToolOutput(content)

        if ctx.tool_result_max_chars is not None and args.offset is None and args.limit is None:
            try:
                st = await asyncio.to_thread(full_path.stat)
            except OSError as exc:
                raise ToolFailure(f"{rel} could not be read: {exc.strerror or exc}") from None

            if st.st_size > cap:
                line_count, is_binary = await asyncio.to_thread(
                    _count_lines_and_check_binary, full_path
                )
                if is_binary:
                    return _out(f"{rel} is a binary file; its contents are not shown.")
                raise ToolFailure(
                    f"{rel} is too large to read whole ({st.st_size} bytes, {line_count} lines; cap is {cap} characters). "
                    "Read it in parts with offset and limit, or search it with Grep."
                )

        try:
            lines, binary, more = await asyncio.to_thread(
                _read_lines, full_path, offset, args.limit or MAX_LINES, cap
            )
        except OSError as exc:
            raise ToolFailure(f"{rel} could not be read: {exc.strerror or exc}") from None

        if ctx.task is not None and self._tasks is not None:
            sha256 = await asyncio.to_thread(_compute_sha256, full_path)
            await self._tasks.record_read(ctx.project_id, ctx.task, rel, sha256)

        if binary:
            return _out(f"{rel} is a binary file; its contents are not shown.")
        if not lines:
            return _out(
                f"{rel} has no lines from line {offset}." if offset > 1 else f"{rel} is empty."
            )
        text = "\n".join(lines)
        if len(text) > cap:
            text = cut_text(text, cap)
        elif more:
            last = offset + len(lines) - 1
            text += f"\n[{rel} continues after line {last}. Read again with offset={last + 1} to see more.]"
        return _out(text)
