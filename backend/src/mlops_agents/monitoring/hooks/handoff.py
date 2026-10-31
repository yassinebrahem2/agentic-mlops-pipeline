"""HandoffObserver that registers action hooks from proposal handoffs and reconciles them at startup."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from mlops_agents.monitoring.hooks.contract import Hooks
from mlops_agents.proposals.contract import HandoffObserver, Handoffs, HandoffView

logger = logging.getLogger(__name__)


class HandoffHooks(HandoffObserver):
    def __init__(self, hooks: Hooks, handoffs: Handoffs) -> None:
        self._hooks = hooks
        self._handoffs = handoffs

    async def stored(self, project_id: str, handoff: HandoffView) -> None:
        registered_pairs: set[tuple[str | None, str]] = set()

        # Project-level commands (model=None)
        project_counts: dict[str, int] = {}
        for cmd in handoff.payload.commands:
            count = project_counts.get(cmd.kind, 0) + 1
            project_counts[cmd.kind] = count
            name = cmd.kind if count == 1 else f"{cmd.kind}-{count}"
            await self._hooks.register(
                project_id,
                spec=cmd,
                name=name,
                model=None,
                source="handoff",
            )
            registered_pairs.add((None, name))

        # Model commands (model=model_spec.name)
        for model_spec in handoff.payload.models:
            model_counts: dict[str, int] = {}
            for cmd in model_spec.commands:
                count = model_counts.get(cmd.kind, 0) + 1
                model_counts[cmd.kind] = count
                name = cmd.kind if count == 1 else f"{cmd.kind}-{count}"
                await self._hooks.register(
                    project_id,
                    spec=cmd,
                    name=name,
                    model=model_spec.name,
                    source="handoff",
                )
                registered_pairs.add((model_spec.name, name))

        # Disable any handoff hook no longer present in newer handoff
        existing_hooks = await self._hooks.list(project_id)
        for existing in existing_hooks:
            if (
                existing.source == "handoff"
                and (existing.model, existing.name) not in registered_pairs
                and existing.status != "disabled"
            ):
                await self._hooks.set_enabled(project_id, existing.id, False)

    async def reconcile(self, project_ids: Sequence[str]) -> None:
        for project_id in project_ids:
            try:
                current_handoff = await self._handoffs.current(project_id)
                if current_handoff is not None:
                    await self.stored(project_id, current_handoff)
            except Exception:
                logger.warning(
                    "Failed to reconcile handoff hooks for project %s",
                    project_id,
                    exc_info=True,
                )
