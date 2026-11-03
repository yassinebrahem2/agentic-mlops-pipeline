"""Rule contracts and protocols (I23)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.monitoring.expr import Condition


class RuleTerm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(
        description="ID of the check whose fired results trigger this term (from handoff operations.checks or recent incidents)."
    )
    model: str | None = Field(
        default=None,
        description="Optional model name to match. If omitted, matches results across all models.",
    )
    when: Condition = Field(
        description="Condition expression evaluated against check result record fields to trigger the term."
    )
    window_minutes: int = Field(
        description="Lookback window in minutes over which to evaluate check results."
    )
    quantifier: Literal["latest", "any", "all"] = Field(
        description="Aggregation over window results: 'latest' for the most recent record, 'any' if at least one matches, 'all' if every record matches."
    )
    min_records: int = Field(
        default=1,
        description="Minimum number of check records required in the window before evaluating (at least 1).",
    )


class RuleLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per_incident: int = Field(
        default=1,
        description="Maximum automated actions allowed per incident before requiring human intervention.",
    )
    per_day: int = Field(
        default=1,
        description="Maximum automated actions allowed across all incidents per day.",
    )


class RuleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description="Unique identifier for the mitigation rule (e.g. 'drift-auto-rollback')."
    )
    why: str = Field(
        description="Reason why this rule is proposed and what operational issue it addresses."
    )
    model: str | None = Field(
        default=None,
        description="Optional model name this rule applies to, or None for project-wide rules.",
    )
    terms: list[RuleTerm] = Field(
        description="Trigger terms evaluated against check results; at least one term is required."
    )
    hook_id: str = Field(description="ID of the action hook to execute when trigger terms are met.")
    hook_kind: Literal["rollback", "rerun", "fall_back"] = Field(
        description="Type of remediation action the hook executes: 'rollback', 'rerun', or 'fall_back'."
    )
    limits: RuleLimits = Field(
        description="Rate and count limits to prevent runaway automated actions."
    )


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
    """Propose an automated incident mitigation rule for user approval. Triggers remediation hooks when monitoring signals match."""

    model_config = ConfigDict(extra="forbid")

    spec: RuleSpec = Field(
        description="Rule specification detailing the trigger terms, remediation hook, and safety limits."
    )


class Rules(Protocol):
    async def list(self, project_id: str, model: str | None = None) -> list[RuleView]: ...

    async def store(self, project_id: str, spec: RuleSpec, proposal_id: str) -> RuleView: ...

    async def set_enabled(self, project_id: str, rule_id: str, enabled: bool) -> RuleView: ...
