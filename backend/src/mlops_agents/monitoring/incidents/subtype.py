"""Subtype determination at incident open (S4 §6)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mlops_agents.monitoring.incidents.contract import SignalDraft
from mlops_agents.monitoring.results.contract import parse_source_id

if TYPE_CHECKING:
    from mlops_agents.proposals.payloads import HandoffPayload


def determine_subtype(draft: SignalDraft, payload: HandoffPayload | None) -> str | None:
    """Determines the incident subtype at open.

    - draft.kind == "silence" -> "silence"
    - fired_result matching train ci_dispatch command -> "failed_training"
    - fired_result matching deploy ci_dispatch command -> "bad_deploy"
    - fired_result matching production_signal ci_job -> "failed_scoring"
    - otherwise None
    """
    if draft.kind == "silence":
        return "silence"

    if payload is not None and draft.kind == "fired_result":
        is_ci = False
        source_ref_workflow: str | None = None
        if draft.source_ref:
            try:
                skind, sref, _ = parse_source_id(draft.source_ref)
                if skind == "ci":
                    is_ci = True
                    source_ref_workflow = sref
            except ValueError:
                if draft.source_ref.startswith("ci:"):
                    is_ci = True
                    source_ref_workflow = draft.source_ref.split(":", 1)[1]

        for m in payload.models:
            if draft.model is not None and m.name != draft.model:
                continue

            # 1. train command of type ci_dispatch -> failed_training
            for cmd in m.commands:
                if (
                    cmd.type == "ci_dispatch"
                    and cmd.kind == "train"
                    and cmd.workflow
                    and (
                        draft.subject == cmd.workflow
                        or (is_ci and source_ref_workflow == cmd.workflow)
                    )
                ):
                    return "failed_training"

            # 2. deploy command of type ci_dispatch -> bad_deploy
            for cmd in m.commands:
                if (
                    cmd.type == "ci_dispatch"
                    and cmd.kind == "deploy"
                    and cmd.workflow
                    and (
                        draft.subject == cmd.workflow
                        or (is_ci and source_ref_workflow == cmd.workflow)
                    )
                ):
                    return "bad_deploy"

            # 3. production_signal of kind ci_job -> failed_scoring
            if m.production_signal and m.production_signal.kind == "ci_job":
                sig_ref = m.production_signal.ref
                if draft.subject == sig_ref or (is_ci and source_ref_workflow == sig_ref):
                    return "failed_scoring"

    return None
