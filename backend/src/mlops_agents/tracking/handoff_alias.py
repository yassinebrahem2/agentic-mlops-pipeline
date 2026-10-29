"""Model candidate alias resolution using handoff specifications."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from mlops_agents.credentials.scrub import error_text
from mlops_agents.proposals.contract import Handoffs, HandoffView
from mlops_agents.tracking.constants import PRODUCTION_ALIAS, PRODUCTION_ENVIRONMENT

_log = logging.getLogger(__name__)


async def resolve_candidate_aliases_fn(
    handoffs: Handoffs,
    project_id: str,
    handoff_error_logged: set[str],
) -> Callable[[str], Sequence[str]]:
    handoff: HandoffView | None = None
    try:
        handoff = await handoffs.current(project_id)
    except Exception as exc:
        if project_id not in handoff_error_logged:
            _log.warning("Failed to read handoff for project %s: %s", project_id, error_text(exc))
            handoff_error_logged.add(project_id)
        return lambda _model: [PRODUCTION_ALIAS]

    if handoff is None or not handoff.payload.models:
        return lambda _model: [PRODUCTION_ALIAS]

    specs_by_name = {m.name: m for m in handoff.payload.models}

    def _get_candidates(model_name: str) -> Sequence[str]:
        spec = specs_by_name.get(model_name)
        if spec is None:
            return [PRODUCTION_ALIAS]
        return sorted(
            alias for alias, env in spec.environments.items() if env == PRODUCTION_ENVIRONMENT
        )

    return _get_candidates
