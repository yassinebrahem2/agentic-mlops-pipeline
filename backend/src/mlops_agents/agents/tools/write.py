from __future__ import annotations

import asyncio
import difflib
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.constants import cap_diff
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
from mlops_agents.agents.tools.file_ops import (
    compute_sha256,
    get_current_task,
    pause_task,
    pop_pending_note,
    resolve_and_validate,
    safe_write_bytes,
)

if TYPE_CHECKING:
    from mlops_agents.agents.tasks.contract import Tasks
    from mlops_agents.sandbox.contract import Sandbox


class WriteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_path: str = Field(
        description="Path of the file to write, relative to the task worktree root."
    )
    content: str = Field(description="Content to write into the file.")


class WriteTool(AgentTool[WriteArgs]):
    name: ClassVar[str] = "Write"
    description: ClassVar[str] = (
        "Write a file in the current task's worktree. Overwriting an existing file requires reading it first."
    )
    risk: ClassVar[RiskClass] = RiskClass.WRITE_CODE
    args_model: ClassVar[type[BaseModel]] = WriteArgs

    def __init__(self, tasks: Tasks, sandbox: Sandbox | None = None) -> None:
        self._tasks = tasks
        self._sandbox = sandbox if sandbox is not None else getattr(tasks, "sandbox", None)

    async def _validate_and_prepare(
        self, ctx: ToolContext, args: WriteArgs, current: TaskRef
    ) -> tuple[Path, str, Path, bytes, str]:
        candidate, rel, resolved_worktree = resolve_and_validate(current.worktree, args.file_path)

        if candidate.exists():
            recorded = await self._tasks.read_hash(ctx.project_id, current, rel)
            if recorded is None:
                raise ToolFailure(f"Read {rel} first.")
            current_hash = await asyncio.to_thread(compute_sha256, candidate)
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
        return candidate, rel, resolved_worktree, new_bytes, diff_text

    async def preview(self, ctx: ToolContext, args: WriteArgs) -> Preview:
        current = await get_current_task(ctx, self._tasks)
        _, rel, _, _, diff_text = await self._validate_and_prepare(ctx, args, current)
        return Preview(kind="diff", title=f"Write {rel}", body=diff_text)

    async def run(self, ctx: ToolContext, args: WriteArgs) -> ToolOutput:
        current = await get_current_task(ctx, self._tasks)
        note = pop_pending_note(self._tasks, ctx.project_id, current.id)
        async with pause_task(self._sandbox, ctx.project_id, current.id):
            (
                candidate,
                rel,
                resolved_worktree,
                new_bytes,
                diff_text,
            ) = await self._validate_and_prepare(ctx, args, current)

            try:
                await self._tasks.claim(ctx.project_id, current, [rel])
            except ClaimConflict as exc:
                raise ToolFailure(str(exc)) from None

            safe_write_bytes(candidate, resolved_worktree, new_bytes)
            ctx.files.invalidate()
            content = cap_diff(diff_text)
            if note:
                content = f"{content}\n\n{note}"
            return ToolOutput(content=content)
