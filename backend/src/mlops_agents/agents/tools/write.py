from __future__ import annotations

import asyncio
import difflib
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.tasks.contract import ClaimConflict
from mlops_agents.agents.tools.base import (
    AgentTool,
    Preview,
    RiskClass,
    TaskRef,
    ToolContext,
    ToolFailure,
    ToolOutput,
)
from mlops_agents.agents.tools.files import is_secret

if TYPE_CHECKING:
    from mlops_agents.agents.tasks.contract import Tasks

CHUNK_SIZE = 65536
MAX_DIFF_CHARS = 30_000


class WriteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_path: str = Field(
        description="Path of the file to write, relative to the task worktree root."
    )
    content: str = Field(description="Content to write into the file.")


def _compute_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def _cap_diff(diff: str) -> str:
    if len(diff) <= MAX_DIFF_CHARS:
        return diff
    cut = len(diff) - MAX_DIFF_CHARS
    return diff[:15_000] + f"\n\n[... diff cut: {cut} characters omitted ...]\n\n" + diff[-15_000:]


def _resolve_and_validate(worktree_root: Path, file_path: str) -> tuple[Path, str, Path]:
    if not file_path or not file_path.strip():
        raise ToolFailure("file_path cannot be empty.")

    resolved_worktree = worktree_root.resolve()
    candidate = Path(file_path)
    if not candidate.is_absolute():
        candidate = worktree_root / candidate

    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(resolved_worktree):
        raise ToolFailure(
            "That path is outside the worktree. Only files inside the task worktree can be modified."
        )

    rel = resolved.relative_to(resolved_worktree).as_posix()
    if not rel or rel == ".":
        raise ToolFailure("file_path must refer to a file, not the root directory.")

    if candidate.is_symlink():
        if not resolved.is_relative_to(resolved_worktree):
            raise ToolFailure(f"Symlink {candidate.name} escapes the worktree.")
        raise ToolFailure(f"Cannot write directly to symlink {candidate.name}.")

    cur = candidate.parent
    while cur != resolved_worktree and cur != cur.parent:
        if cur.is_symlink() and not cur.resolve().is_relative_to(resolved_worktree):
            raise ToolFailure(f"Path escapes worktree through symlink {cur.name}.")
        cur = cur.parent

    if is_secret(rel):
        raise ToolFailure(f"{rel} looks like a secret file, so it cannot be written.")

    return candidate, rel, resolved_worktree


class WriteTool(AgentTool[WriteArgs]):
    name: ClassVar[str] = "Write"
    description: ClassVar[str] = (
        "Write a file in the current task's worktree. Overwriting an existing file requires reading it first."
    )
    risk: ClassVar[RiskClass] = RiskClass.WRITE_CODE
    args_model: ClassVar[type[BaseModel]] = WriteArgs

    def __init__(self, tasks: Tasks) -> None:
        self._tasks = tasks

    async def _get_current_task(self, ctx: ToolContext) -> TaskRef:
        current = ctx.task or await self._tasks.current(ctx.project_id, ctx.session_id)
        if current is None:
            raise ToolFailure("Start a task first with StartTask.")
        return current

    async def _validate_and_prepare(
        self, ctx: ToolContext, args: WriteArgs
    ) -> tuple[TaskRef, Path, str, Path, bytes, str]:
        current = await self._get_current_task(ctx)
        candidate, rel, resolved_worktree = _resolve_and_validate(current.worktree, args.file_path)

        if candidate.exists():
            recorded = await self._tasks.read_hash(ctx.project_id, current, rel)
            if recorded is None:
                raise ToolFailure(f"Read {rel} first.")
            current_hash = await asyncio.to_thread(_compute_sha256, candidate)
            if current_hash != recorded:
                raise ToolFailure(f"{rel} changed since you read it.")

            old_bytes = candidate.read_bytes()
            is_crlf = b"\r\n" in old_bytes
            old_text = old_bytes.decode("utf-8", "replace")
            old_lines = old_text.splitlines()

            if is_crlf:
                content_normalized = args.content.replace("\r\n", "\n").replace("\n", "\r\n")
            else:
                content_normalized = args.content.replace("\r\n", "\n")
            new_bytes = content_normalized.encode("utf-8")
            new_lines = content_normalized.splitlines()
        else:
            old_lines = []
            content_normalized = args.content
            new_bytes = content_normalized.encode("utf-8")
            new_lines = content_normalized.splitlines()

        diff_lines = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                lineterm="",
            )
        )
        diff_text = "\n".join(diff_lines)
        return current, candidate, rel, resolved_worktree, new_bytes, diff_text

    async def preview(self, ctx: ToolContext, args: WriteArgs) -> Preview:
        _, _, rel, _, _, diff_text = await self._validate_and_prepare(ctx, args)
        return Preview(kind="diff", title=f"Write {rel}", body=diff_text)

    async def run(self, ctx: ToolContext, args: WriteArgs) -> ToolOutput:
        (
            current,
            candidate,
            rel,
            resolved_worktree,
            new_bytes,
            diff_text,
        ) = await self._validate_and_prepare(ctx, args)

        try:
            await self._tasks.claim(ctx.project_id, current, [rel])
        except ClaimConflict as exc:
            raise ToolFailure(str(exc)) from None

        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(new_bytes)

        if not candidate.resolve(strict=False).is_relative_to(resolved_worktree):
            raise ToolFailure("File escaped worktree.")

        ctx.files.invalidate()
        return ToolOutput(content=_cap_diff(diff_text))
