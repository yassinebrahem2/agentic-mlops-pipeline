from __future__ import annotations

import asyncio
import difflib
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
from mlops_agents.agents.tools.file_ops import (
    cap_diff,
    compute_sha256,
    get_current_task,
    pause_task,
    resolve_and_validate,
    safe_write_bytes,
)

if TYPE_CHECKING:
    from mlops_agents.agents.tasks.contract import Tasks
    from mlops_agents.sandbox.contract import Sandbox


class EditArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_path: str = Field(
        description="Path of the file to edit, relative to the task worktree root."
    )
    old_string: str = Field(description="The string to be replaced.")
    new_string: str = Field(description="The replacement string.")
    replace_all: bool = Field(
        default=False,
        description="Replace all occurrences of old_string instead of only the first.",
    )


class EditTool(AgentTool[EditArgs]):
    name: ClassVar[str] = "Edit"
    description: ClassVar[str] = (
        "Replace text in an existing file in the current task's worktree. Requires reading the file first."
    )
    risk: ClassVar[RiskClass] = RiskClass.WRITE_CODE
    args_model: ClassVar[type[BaseModel]] = EditArgs

    def __init__(self, tasks: Tasks, sandbox: Sandbox | None = None) -> None:
        self._tasks = tasks
        self._sandbox = sandbox if sandbox is not None else getattr(tasks, "sandbox", None)

    async def _validate_and_prepare(
        self, ctx: ToolContext, args: EditArgs
    ) -> tuple[TaskRef, Path, str, Path, bytes, str]:
        if not args.old_string:
            raise ToolFailure("old_string cannot be empty.")

        current = await get_current_task(ctx, self._tasks)
        candidate, rel, resolved_worktree = resolve_and_validate(current.worktree, args.file_path)

        if not candidate.exists():
            raise ToolFailure(f"{rel} does not exist.")

        recorded = await self._tasks.read_hash(ctx.project_id, current, rel)
        if recorded is None:
            raise ToolFailure(f"Read {rel} first.")
        current_hash = await asyncio.to_thread(compute_sha256, candidate)
        if current_hash != recorded:
            raise ToolFailure(f"{rel} changed since you read it.")

        old_bytes = candidate.read_bytes()
        is_crlf = b"\r\n" in old_bytes
        try:
            file_text = old_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolFailure(
                f"{rel} is not a valid UTF-8 text file, so it cannot be edited."
            ) from None

        target_old = args.old_string
        if is_crlf and target_old not in file_text:
            crlf_old = target_old.replace("\r\n", "\n").replace("\n", "\r\n")
            if crlf_old in file_text:
                target_old = crlf_old
        elif not is_crlf and target_old not in file_text:
            lf_old = target_old.replace("\r\n", "\n")
            if lf_old in file_text:
                target_old = lf_old

        count = file_text.count(target_old)
        if count == 0:
            raise ToolFailure(f"old_string not found in {rel}.")
        if count > 1 and not args.replace_all:
            raise ToolFailure(
                f"old_string appears {count} times in {rel}. Use replace_all=True to replace all occurrences."
            )

        target_new = args.new_string
        if is_crlf:
            target_new = target_new.replace("\r\n", "\n").replace("\n", "\r\n")
        else:
            target_new = target_new.replace("\r\n", "\n")

        if args.replace_all:
            new_text = file_text.replace(target_old, target_new)
        else:
            new_text = file_text.replace(target_old, target_new, 1)

        new_bytes = new_text.encode("utf-8")
        old_lines = file_text.splitlines()
        new_lines = new_text.splitlines()

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

    async def preview(self, ctx: ToolContext, args: EditArgs) -> Preview:
        _, _, rel, _, _, diff_text = await self._validate_and_prepare(ctx, args)
        return Preview(kind="diff", title=f"Edit {rel}", body=diff_text)

    async def run(self, ctx: ToolContext, args: EditArgs) -> ToolOutput:
        current = await get_current_task(ctx, self._tasks)
        async with pause_task(self._sandbox, ctx.project_id, current.id):
            (
                _,
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

            safe_write_bytes(candidate, resolved_worktree, new_bytes)
            ctx.files.invalidate()
            return ToolOutput(content=cap_diff(diff_text))
