"""Fixes, training requests and policies contracts and protocols (I34, I35)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

FixChoice = Literal["re_threshold", "recalibrate", "retrain", "decline"]
FixStatus = Literal["requested", "chosen", "running", "done", "declined", "escalated"]


class FixRequestView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    incident_id: str
    model: str
    requested_by: str
    evidence: str
    choice: FixChoice | None = None
    why: str | None = None
    why_not: dict[str, str] = {}
    status: FixStatus
    outcome: Literal["pending", "cleared", "not_cleared"]
    created: datetime


class GuardRefusal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal[
        "data_held",
        "open_data_quality_incident",
        "labels_immature",
        "policy_limit",
        "circuit_open",
    ]
    detail: str


class GuardResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    refusals: list[GuardRefusal]


class TrainingWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime


class CircuitState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open: bool
    failed_fixes: int
    window_days: int


class Fixes(Protocol):
    async def request(
        self, project_id: str, incident_id: str, agent: str, evidence: str
    ) -> FixRequestView: ...

    async def choose(
        self,
        project_id: str,
        fix_id: str,
        agent: str,
        choice: FixChoice,
        why: str,
        why_not: dict[str, str],
    ) -> FixRequestView: ...

    async def guards(
        self,
        project_id: str,
        model: str,
        window: TrainingWindow,
        policy_id: str | None,
    ) -> GuardResult: ...

    async def circuit(self, project_id: str, model: str) -> CircuitState: ...

    async def get(self, project_id: str, fix_id: str) -> FixRequestView | None: ...


class WindowRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["trailing_days", "since_last_run", "fixed"]
    days: int | None = None
    start: datetime | None = None
    end: datetime | None = None


class TrainingPolicyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    train_hook_id: str
    evaluate_hook_id: str | None = None
    window: WindowRule
    min_interval_hours: int
    max_runs_per_week: int
    run_time_limit_minutes: int


class TrainingPolicyView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    payload: TrainingPolicyPayload
    status: Literal["active", "suspended", "disabled"]
    suspended_reason: str | None = None
    proposal_id: str
    approved: datetime


class TrainingRequestView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    fix_id: str | None = None
    model: str
    hook_id: str
    policy_id: str | None = None
    window: TrainingWindow | None = None
    mode: Literal["train", "evaluate"]
    status: Literal["started", "linked", "failed", "silent"]
    run_key: str | None = None
    ci_run_url: str | None = None
    started: datetime


class Training(Protocol):
    async def policies(
        self, project_id: str, model: str | None = None
    ) -> list[TrainingPolicyView]: ...

    async def request(self, project_id: str, request_id: str) -> TrainingRequestView | None: ...

    async def requests(
        self, project_id: str, model: str, limit: int = 50
    ) -> list[TrainingRequestView]: ...
