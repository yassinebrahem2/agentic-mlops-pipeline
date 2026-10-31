"""Rule-based severity policy implementation (S4 §7)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from mlops_agents.monitoring.expectations.contract import SECOND_DEADLINE_KEY
from mlops_agents.monitoring.incidents.contract import (
    IncidentView,
    Severity,
    SeverityDecision,
    SignalView,
    stages_of,
)

if TYPE_CHECKING:
    from mlops_agents.monitoring.incidents.contract import Incidents
    from mlops_agents.proposals.contract import Handoffs
    from mlops_agents.proposals.payloads import HandoffPayload, PipelineEdge

SEVERITY_RANKS: Final[dict[Severity, int]] = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

RANK_TO_SEVERITY: Final[dict[int, Severity]] = {
    1: "low",
    2: "medium",
    3: "high",
    4: "critical",
}


def parse_cause_key(c_key: str) -> tuple[str, str | None]:
    """Parses a collision-resistant cause key formatted as <len>:<subject>:<model>."""
    parts = c_key.split(":", 1)
    if len(parts) == 2 and parts[0].isdigit():
        length = int(parts[0])
        subject = parts[1][:length]
        remainder = parts[1][length:]
        if remainder.startswith(":"):
            remainder = remainder[1:]
        if remainder.startswith("m:"):
            return subject, remainder[2:]
        return subject, None
    return "", None


def _model_stages(payload: HandoffPayload) -> set[str]:
    """Finds all pipeline stages where a model is an input or output."""
    model_stages: set[str] = set()
    for stage in payload.pipeline:
        for m in payload.models:
            if (
                m.name in stage.inputs
                or m.name in stage.outputs
                or (
                    m.predictions is not None
                    and (
                        m.predictions.table in stage.inputs or m.predictions.table in stage.outputs
                    )
                )
                or (
                    m.labels is not None
                    and (m.labels.table in stage.inputs or m.labels.table in stage.outputs)
                )
                or "model" in stage.inputs
                or "model" in stage.outputs
            ):
                model_stages.add(stage.name)
                break
    return model_stages


def has_path_to_model_stage(
    feed_stages: list[str],
    model_stages: set[str],
    edges: list[PipelineEdge],
) -> bool:
    """Checks whether there is a directed path from any feed stage to any model stage."""
    for s in feed_stages:
        if s in model_stages:
            return True

    adj: dict[str, list[str]] = {}
    for edge in edges:
        adj.setdefault(edge.from_stage, []).append(edge.to_stage)

    visited: set[str] = set(feed_stages)
    queue: list[str] = list(feed_stages)

    while queue:
        curr = queue.pop(0)
        for next_stage in adj.get(curr, []):
            if next_stage in model_stages:
                return True
            if next_stage not in visited:
                visited.add(next_stage)
                queue.append(next_stage)

    return False


class RuleSeverity:
    """Deterministic, code-only severity policy per S4 §7."""

    def __init__(self, handoffs: Handoffs, incidents: Incidents | None = None) -> None:
        self._handoffs = handoffs
        self._incidents = incidents

    def bind_incidents(self, incidents: Incidents) -> None:
        self._incidents = incidents

    async def decide(self, project_id: str, incident: IncidentView) -> SeverityDecision:
        severity: Severity = "medium"
        reasons: list[str] = []

        # Rule 1 (Subtype): failed_scoring or bad_deploy -> critical
        if incident.subtype in ("failed_scoring", "bad_deploy"):
            severity = "critical"
            reasons.append(f"Subtype '{incident.subtype}' sets severity to critical")

        # Rule 2 (Silence of feed or job that production depends on)
        signals: list[SignalView] = []
        if self._incidents is not None:
            try:
                signals = await self._incidents.signals(project_id, incident.id)
            except Exception:
                signals = []

        is_silence = incident.subtype == "silence" or any(s.kind == "silence" for s in signals)
        if is_silence:
            subject, _ = parse_cause_key(incident.cause_key)
            if subject:
                handoff = await self._handoffs.current(project_id)
                if handoff is not None:
                    payload = handoff.payload
                    feed_stages = stages_of(subject, payload)
                    model_stages = _model_stages(payload)
                    if (
                        feed_stages
                        and model_stages
                        and has_path_to_model_stage(feed_stages, model_stages, payload.edges)
                    ):
                        has_second_deadline = any(
                            bool(s.detail.get(SECOND_DEADLINE_KEY)) for s in signals
                        )
                        if has_second_deadline:
                            severity = "critical"
                            reasons.append(
                                "Second deadline missed for feed or job that production depends on"
                            )
                        else:
                            if SEVERITY_RANKS[severity] < SEVERITY_RANKS["high"]:
                                severity = "high"
                            reasons.append("Silence of feed or job that production depends on")

        # Rule 3 (Cost ratio): model_performance with cost_ratio > 1.0 raises severity one level
        if incident.category == "model_performance":
            handoff = await self._handoffs.current(project_id)
            if handoff is not None:
                payload = handoff.payload
                target_model = next(
                    (m for m in payload.models if m.name == incident.model),
                    None,
                )
                if target_model is None and len(payload.models) == 1 and incident.model is None:
                    target_model = payload.models[0]

                if (
                    target_model is not None
                    and target_model.profile.cost_ratio is not None
                    and target_model.profile.cost_ratio > 1.0
                ):
                    cur_rank = SEVERITY_RANKS[severity]
                    new_rank = min(cur_rank + 1, 4)
                    severity = RANK_TO_SEVERITY[new_rank]
                    reasons.append(
                        f"Model performance error cost ratio ({target_model.profile.cost_ratio}) raises severity"
                    )

        if not reasons:
            reasons = ["Base severity"]

        return SeverityDecision(severity=severity, reasons=reasons)
