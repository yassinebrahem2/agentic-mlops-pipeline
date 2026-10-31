"""Rule contracts and protocols (I23)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from mlops_agents.monitoring.expr import Condition


class RuleTerm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str
    model: str | None = None
    when: Condition
    window_minutes: int
    quantifier: Literal["latest", "any", "all"]
    min_records: int = 1


class RuleLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per_incident: int = 1
    per_day: int = 1


class RuleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    why: str
    model: str | None = None
    terms: list[RuleTerm]
    hook_id: str
    hook_kind: Literal["rollback", "rerun", "fall_back"]
    limits: RuleLimits


def validate_rule_spec(spec: RuleSpec) -> None:
    """Validate rule specification terms and limits."""
    if not spec.terms:
        raise ValueError("Rule spec terms cannot be empty; at least one term is required.")
    for i, term in enumerate(spec.terms):
        if term.min_records < 1:
            raise ValueError(f"Term {i} min_records must be at least 1 (got {term.min_records}).")
        if term.window_minutes < 1:
            raise ValueError(
                f"Term {i} window_minutes must be at least 1 (got {term.window_minutes})."
            )
    if spec.limits.per_incident < 1:
        raise ValueError(
            f"Rule limits per_incident must be at least 1 (got {spec.limits.per_incident})."
        )
    if spec.limits.per_day < 1:
        raise ValueError(f"Rule limits per_day must be at least 1 (got {spec.limits.per_day}).")


class RuleView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    spec: RuleSpec
    enabled: bool
    proposal_id: str
    approved: datetime


class RulePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: RuleSpec


class Rules(Protocol):
    async def list(self, project_id: str, model: str | None = None) -> list[RuleView]: ...

    async def store(self, project_id: str, spec: RuleSpec, proposal_id: str) -> RuleView: ...

    async def set_enabled(self, project_id: str, rule_id: str, enabled: bool) -> RuleView: ...
