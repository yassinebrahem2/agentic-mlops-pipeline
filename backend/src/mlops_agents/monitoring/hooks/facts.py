"""Handoff-backed GateFacts implementation (Task 7, #492)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mlops_agents.agents.approvals.authority import GateFacts

if TYPE_CHECKING:
    from mlops_agents.monitoring.hooks.contract import Hooks
    from mlops_agents.proposals.contract import Handoffs


class HandoffGateFacts(GateFacts):
    """Derives approval gate facts from hook status and the current proposal handoff."""

    def __init__(self, hooks: Hooks, handoffs: Handoffs) -> None:
        self._hooks = hooks
        self._handoffs = handoffs

    async def facts(
        self, project_id: str, hook_id: str | None
    ) -> tuple[bool | None, bool | None, bool | None]:
        if hook_id is None:
            return (None, None, None)

        hook = await self._hooks.get(project_id, hook_id)
        if hook is None:
            return (None, None, None)

        handoff = await self._handoffs.current(project_id)
        if handoff is None:
            return (None, None, None)

        deploys = True if hook.status == "suspended" else hook.spec.deploys
        model_spec = (
            next((m for m in handoff.payload.models if m.name == hook.model), None)
            if hook.model
            else None
        )
        has_production_signal = bool(model_spec and model_spec.production_signal)
        team_gate_authority = bool(model_spec and model_spec.promotion == "team_gate")

        return (deploys, has_production_signal, team_gate_authority)
