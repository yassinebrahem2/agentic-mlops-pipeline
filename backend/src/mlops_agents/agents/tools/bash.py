"""Bash tool for running commands in the task Docker sandbox container."""

from __future__ import annotations

import string
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.approvals.authority import ActionCall
from mlops_agents.agents.tools.base import (
    AgentTool,
    Preview,
    RiskClass,
    ToolContext,
    ToolFailure,
    ToolOutput,
)
from mlops_agents.sandbox.contract import (
    DEFAULT_BASH_TIMEOUT_S,
    MAX_BASH_TIMEOUT_S,
    Sandbox,
    SandboxUnavailable,
)

if TYPE_CHECKING:
    from mlops_agents.monitoring.hooks.contract import Hooks

MAX_TITLE_CMD_LEN = 60


class BashArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(description="The command to run in the container via bash -lc.")
    timeout: int | None = Field(
        default=None,
        ge=1,
        le=MAX_BASH_TIMEOUT_S,
        description=f"Timeout in seconds (default {DEFAULT_BASH_TIMEOUT_S}, max {MAX_BASH_TIMEOUT_S}).",
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

    def __init__(self, sandbox: Sandbox | None = None, hooks: Hooks | None = None) -> None:
        self._sandbox = sandbox
        self._hooks = hooks

    async def action_call(self, args: BashArgs, ctx: ToolContext) -> ActionCall | None:
        if self._hooks is None:
            return None
        hooks = await self._hooks.list(ctx.project_id)
        cmd_norm = " ".join(args.command.split())
        for hook in hooks:
            if (
                hook.status == "disabled"
                or not hook.spec.deploys
                or hook.spec.type != "command"
                or hook.spec.template is None
            ):
                continue
            prefix_parts: list[str] = []
            try:
                for literal_text, field_name, _, _ in string.Formatter().parse(hook.spec.template):
                    if literal_text:
                        prefix_parts.append(literal_text)
                    if field_name is not None:
                        break
            except ValueError:
                continue
            prefix = "".join(prefix_parts)
            prefix_norm = " ".join(prefix.split())
            if not prefix_norm:
                continue
            if prefix_norm in cmd_norm:
                return ActionCall(
                    project_id=ctx.project_id,
                    agent=ctx.agent,
                    kind=hook.spec.kind,
                    hook_id=hook.id,
                    model=hook.model,
                    incident_id=None,
                    rule_id=None,
                    policy_id=None,
                    deploys=None,
                    has_production_signal=None,
                    team_gate_authority=None,
                )
        return None

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

        timeout_s = args.timeout if args.timeout is not None else DEFAULT_BASH_TIMEOUT_S
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
            return ToolOutput(res.output, is_error=True)
        if res.exit_code is not None and res.exit_code != 0:
            prefix = f"Command failed with exit code {res.exit_code}"
            msg = f"{prefix}:\n{res.output}" if res.output else f"{prefix}."
            return ToolOutput(msg, is_error=True)
        return ToolOutput(res.output)
