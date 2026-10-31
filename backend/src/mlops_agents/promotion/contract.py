"""Evaluation, reports, promotions, deployments and merge contracts (I37, I38, I40)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from mlops_agents.tracking.contract import EvaluationRecord
from mlops_agents.training.contract import TrainingWindow


class ParityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tolerance: float
    mismatch_rate: float


class MetricValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float
    ci_low: float | None = None
    ci_high: float | None = None
    n: int


class MetricSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overall: dict[str, MetricValue]
    per_group: dict[str, dict[str, MetricValue]]


class EvaluationPayload(BaseModel):
    # extra="allow" so a team's extra fields survive (I37)
    model_config = ConfigDict(extra="allow")

    schema_version: int
    evaluation_commit: str
    gate_config_version: str
    champion_version: str | None = None
    decision: Literal["promote", "hold", "reject"] | None = None
    reasons: list[str] | None = None
    parity: ParityResult | None = None
    champion_metrics: MetricSet | None = None
    challenger_metrics: MetricSet | None = None
    calibration: dict[str, Any] | None = None
    expected_cost: float | None = None
    decision_rate: float | None = None
    holdout_window: TrainingWindow | None = None
    training_window: TrainingWindow | None = None
    serving_compatibility: dict[str, Any] | None = None
    computed_where: str | None = None


class EvaluationView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record: EvaluationRecord
    payload: EvaluationPayload
    not_computed: dict[str, str | None]


class EvaluationReader(Protocol):
    async def for_candidate(
        self, project_id: str, model: str, version: str
    ) -> EvaluationView | None: ...

    async def latest(self, project_id: str, model: str) -> EvaluationView | None: ...


class ReportField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str | None = None
    not_applicable_reason: str | None = None

    @model_validator(mode="after")
    def _validate_one_set(self) -> ReportField:
        if (self.value is None and self.not_applicable_reason is None) or (
            self.value is not None and self.not_applicable_reason is not None
        ):
            raise ValueError("ReportField must have exactly one of value or not_applicable_reason")
        return self


class ReportDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    candidate_version: str
    fix_id: str | None = None
    training_request_id: str | None = None
    assessments: dict[str, ReportField]


class ReportView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    draft: ReportDraft
    evaluation: EvaluationView | None = None
    created: datetime
    author: str


class Reports(Protocol):
    async def save(self, project_id: str, draft: ReportDraft, author: str) -> ReportView: ...

    async def get(self, project_id: str, report_id: str) -> ReportView | None: ...

    async def for_candidate(
        self, project_id: str, model: str, version: str
    ) -> ReportView | None: ...


class PromotionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    report_id: str
    candidate_version: str
    champion_version: str | None = None
    deploy_hook_id: str


class PromotionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    model: str
    candidate_version: str
    champion_version: str | None = None
    mode: Literal["team_gate", "person"]
    decided_by: Literal["person", "agent_bypass", "team_gate"]
    status: Literal["proposed", "applied", "refused", "rejected"]
    report_id: str | None = None
    refusals: list[str] = []
    created: datetime
    applied_at: datetime | None = None


class Promotions(Protocol):
    async def validate(self, project_id: str, payload: PromotionPayload) -> list[str]: ...

    async def history(self, project_id: str, model: str | None = None) -> list[PromotionView]: ...


# I38 Merging types
class ProtectionSurfaces(Protocol):
    async def paths(self, project_id: str) -> list[str]: ...

    async def touches(self, project_id: str, pr_number: int) -> list[str]: ...


class MergeCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    refusals: list[str]


# I40 Deployments data types
class EnvironmentRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    environment: str
    alias: str
    version: str | None = None


class DecisionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: datetime
    model: str
    kind: Literal["promotion", "rollback", "fall_back"]
    from_version: str | None = None
    to_version: str | None = None
    decided_by: Literal["person", "team_gate", "rule", "agent_bypass"]
    decided_by_ref: str | None = None
    report_id: str | None = None


class OpenPipelinePr(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int
    title: str
    agent: str
    labels: list[str]


class DeploymentsView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environments: list[EnvironmentRow]
    decisions: list[DecisionRow]
    open_prs: list[OpenPipelinePr]
