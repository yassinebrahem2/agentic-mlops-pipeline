"""RunHook tool (I33)."""

from __future__ import annotations

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
from mlops_agents.agents.tools.names import RUN_HOOK
from mlops_agents.monitoring.hooks.contract import HookCall, HookRefused
from mlops_agents.monitoring.untrusted import untrusted

if TYPE_CHECKING:
    from mlops_agents.monitoring.hooks.contract import Hooks


class RunHookArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hook_id: str = Field(description="ID of the hook to run.")
    reason: str = Field(description="Reason for running this hook.")
    incident_id: str | None = Field(
        default=None, description="Incident ID if associated with an incident."
    )
    fix_id: str | None = Field(default=None, description="Fix ID if associated with a fix.")
    params: dict[str, str] = Field(
        default_factory=dict, description="Parameters to pass to the hook."
    )
    rule_id: str | None = Field(default=None, description="Rule ID authorizing this run.")
    policy_id: str | None = Field(default=None, description="Policy ID authorizing this run.")


class RunHookTool(AgentTool[RunHookArgs]):
    name: ClassVar[str] = RUN_HOOK
    description: ClassVar[str] = (
        "Run an action hook that has been approved or authorized by policy/rule."
    )
    risk: ClassVar[RiskClass] = RiskClass.DEPLOY
    args_model: ClassVar[type[BaseModel]] = RunHookArgs

    def __init__(self, hooks: Hooks) -> None:
        self._hooks = hooks

    async def action_call(self, args: RunHookArgs, ctx: ToolContext) -> ActionCall | None:
        hook = await self._hooks.get(ctx.project_id, args.hook_id)
        if hook is None:
            raise ToolFailure(f"Unknown hook {args.hook_id!r}")
        return ActionCall(
            project_id=ctx.project_id,
            agent=ctx.agent,
            kind=hook.spec.kind,
            hook_id=args.hook_id,
            model=hook.model,
            incident_id=args.incident_id,
            rule_id=args.rule_id,
            policy_id=args.policy_id,
            deploys=None,
            has_production_signal=None,
            team_gate_authority=None,
        )

    async def preview(self, ctx: ToolContext, args: RunHookArgs) -> Preview:
        hook = await self._hooks.get(ctx.project_id, args.hook_id)
        if hook is None:
            raise ToolFailure(f"Unknown hook {args.hook_id!r}")
        return Preview(
            kind="command",
            title=f"Run hook {hook.name} ({hook.id})",
            body=args.model_dump_json(indent=2),
        )

    async def run(self, ctx: ToolContext, args: RunHookArgs) -> ToolOutput:
        if ctx.authority is None:
            raise RuntimeError("RunHook reached without authority")
        hook = await self._hooks.get(ctx.project_id, args.hook_id)
        if hook is None:
            raise ToolFailure(f"Unknown hook {args.hook_id!r}")

        declared = set(hook.spec.placeholders)
        extra_params = [k for k in sorted(args.params) if k not in declared]
        if extra_params:
            raise ToolFailure(
                f"Undeclared parameters for hook {args.hook_id!r}: {', '.join(extra_params)}"
            )

        call = HookCall(
            hook_id=args.hook_id,
            reason=args.reason,
            incident_id=args.incident_id,
            fix_id=args.fix_id,
            params=args.params,
            rule_id=args.rule_id,
            policy_id=args.policy_id,
        )
        try:
            res = await self._hooks.run(ctx.project_id, call, ctx.authority)
        except HookRefused as exc:
            return ToolOutput(f"Hook {args.hook_id!r} refused: {exc}", is_error=True)

        if not res.ok:
            return ToolOutput(
                f"Hook {args.hook_id!r} failed:\n{untrusted(res.detail, source='hook_run')}",
                is_error=True,
            )
        return ToolOutput(untrusted(res.detail, source="hook_run"), is_error=False)
