"""Bash tool for running commands in the task Docker sandbox container."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.tools.base import (
    AgentTool,
    Preview,
    RiskClass,
    ToolContext,
    ToolFailure,
    ToolOutput,
)
from mlops_agents.sandbox.contract import Sandbox, SandboxUnavailable

MAX_TITLE_CMD_LEN = 60


class BashArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(description="The command to run in the container via bash -lc.")
    timeout: int | None = Field(
        default=None,
        ge=1,
        le=600,
        description="Timeout in seconds (default 120, max 600).",
    )
    description: str | None = Field(
        default=None,
        description="Optional brief description of what the command does.",
    )


class BashTool(AgentTool[BashArgs]):
    name: ClassVar[str] = "Bash"
    description: ClassVar[str] = "Run a command in the task's Docker sandbox container."
    risk: ClassVar[RiskClass] = RiskClass.RUN
    args_model: ClassVar[type[BaseModel]] = BashArgs

    def __init__(self, sandbox: Sandbox | None = None) -> None:
        self._sandbox = sandbox

    async def preview(self, ctx: ToolContext, args: BashArgs) -> Preview:
        cmd = args.command.strip()
        first_line = cmd.splitlines()[0] if cmd else ""
        if len(first_line) > MAX_TITLE_CMD_LEN:
            first_line = f"{first_line[: MAX_TITLE_CMD_LEN - 3]}..."
        title = f"Run `{first_line}`" if first_line else "Run command"
        return Preview(
            kind="command",
            title=title,
            body=args.command,
        )

    async def run(self, ctx: ToolContext, args: BashArgs) -> ToolOutput:
        if ctx.task is None:
            raise ToolFailure("Start a task first with StartTask.")
        if self._sandbox is None:
            raise ToolFailure("Sandbox is not available.")

        timeout_s = args.timeout or 120
        try:
            res = await self._sandbox.run(
                ctx.project_id,
                ctx.task,
                args.command,
                timeout_s=timeout_s,
            )
        except SandboxUnavailable as exc:
            raise ToolFailure(str(exc)) from exc
        finally:
            ctx.files.invalidate()

        if res.timed_out:
            return ToolOutput(res.output)
        if res.exit_code is not None and res.exit_code != 0:
            prefix = f"Command failed with exit code {res.exit_code}"
            msg = f"{prefix}:\n{res.output}" if res.output else f"{prefix}."
            return ToolOutput(msg)
        return ToolOutput(res.output)
