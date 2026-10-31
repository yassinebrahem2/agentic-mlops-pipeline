"""Hold tool (I33)."""

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
from mlops_agents.agents.tools.names import HOLD
from mlops_agents.monitoring.hooks.contract import HookCall, HookRefused
from mlops_agents.monitoring.incidents.contract import HoldScope
from mlops_agents.monitoring.untrusted import untrusted

if TYPE_CHECKING:
    from mlops_agents.monitoring.hooks.contract import Hooks, HookView
    from mlops_agents.monitoring.incidents.contract import Incidents


class HoldArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(description="Incident ID to place on hold.")
    scope: HoldScope = Field(description="Scope of data tables and time window to hold.")
    reason: str = Field(description="Reason for recording the hold.")


class HoldTool(AgentTool[HoldArgs]):
    name: ClassVar[str] = HOLD
    description: ClassVar[str] = (
        "Record a data hold on an incident and execute the matching hold hook if available."
    )
    risk: ClassVar[RiskClass] = RiskClass.DEPLOY
    args_model: ClassVar[type[BaseModel]] = HoldArgs

    def __init__(self, hooks: Hooks, incidents: Incidents) -> None:
        self._hooks = hooks
        self._incidents = incidents

    async def _resolve_hold_hooks(
        self, project_id: str, model: str | None
    ) -> tuple[list[HookView], list[HookView]]:
        all_hold = [h for h in await self._hooks.list(project_id) if h.spec.kind == "hold"]
        matching = [h for h in all_hold if h.model == model] if model is not None else []
        if not matching:
            matching = [h for h in all_hold if h.model is None]
        active = [h for h in matching if h.status == "active"]
        return active, matching

    async def _resolve_candidate_hooks(self, project_id: str, model: str | None) -> list[HookView]:
        active, _ = await self._resolve_hold_hooks(project_id, model)
        return active

    async def action_call(self, args: HoldArgs, ctx: ToolContext) -> ActionCall | None:
        incident = await self._incidents.get(ctx.project_id, args.incident_id)
        if incident is None:
            raise ToolFailure(f"Unknown incident {args.incident_id!r}")

        candidates = await self._resolve_candidate_hooks(ctx.project_id, incident.model)
        hook_id = candidates[0].id if len(candidates) == 1 else None

        return ActionCall(
            project_id=ctx.project_id,
            agent=ctx.agent,
            kind="hold",
            hook_id=hook_id,
            model=incident.model,
            incident_id=args.incident_id,
            deploys=None,
            has_production_signal=None,
            team_gate_authority=None,
        )

    async def preview(self, ctx: ToolContext, args: HoldArgs) -> Preview:
        incident = await self._incidents.get(ctx.project_id, args.incident_id)
        if incident is None:
            raise ToolFailure(f"Unknown incident {args.incident_id!r}")
        return Preview(
            kind="command",
            title=f"Hold on incident {args.incident_id}",
            body=args.model_dump_json(indent=2),
        )

    async def run(self, ctx: ToolContext, args: HoldArgs) -> ToolOutput:
        if ctx.authority is None:
            raise RuntimeError("Hold reached without authority")
        incident = await self._incidents.get(ctx.project_id, args.incident_id)
        if incident is None:
            raise ToolFailure(f"Unknown incident {args.incident_id!r}")

        await self._incidents.record_hold(
            ctx.project_id,
            args.incident_id,
            args.scope,
            args.reason,
            by=ctx.agent,
        )

        active_candidates, all_matching = await self._resolve_hold_hooks(
            ctx.project_id, incident.model
        )

        if len(active_candidates) == 0:
            suspended = [h for h in all_matching if h.status == "suspended"]
            if suspended:
                return ToolOutput(
                    f"Hold recorded on incident {args.incident_id}. Hold command {suspended[0].id} is suspended ({suspended[0].suspended_reason or 'suspended'}).",
                    is_error=False,
                )
            return ToolOutput(
                f"Hold recorded on incident {args.incident_id}. No hold command is registered.",
                is_error=False,
            )

        if len(active_candidates) > 1:
            ids = ", ".join(h.id for h in active_candidates)
            return ToolOutput(
                f"Hold recorded on incident {args.incident_id}. Several active hold hooks found for this scope: {ids}. Choose one to run with RunHook.",
                is_error=False,
            )

        hook = active_candidates[0]
        call = HookCall(
            hook_id=hook.id,
            reason=args.reason,
            incident_id=args.incident_id,
        )
        try:
            res = await self._hooks.run(ctx.project_id, call, ctx.authority)
        except HookRefused as exc:
            return ToolOutput(
                f"Hold recorded on incident {args.incident_id}, but hold hook {hook.id!r} was refused: {exc}",
                is_error=True,
            )

        if not res.ok:
            return ToolOutput(
                f"Hold recorded on incident {args.incident_id}, but hold hook {hook.id!r} failed:\n{untrusted(res.detail, source='hook_run')}",
                is_error=True,
            )

        return ToolOutput(
            f"Hold recorded on incident {args.incident_id}.\n{untrusted(res.detail, source='hook_run')}",
            is_error=False,
        )
