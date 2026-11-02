"""Incidents, signals, severity, routing and waker contracts (I26, I27)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from mlops_agents.monitoring.expectations.contract import ExpectationKey
from mlops_agents.monitoring.results.contract import ResultCategory
from mlops_agents.monitoring.verdicts.contract import VerdictView
from mlops_agents.tracking.contract import Page

if TYPE_CHECKING:
    from mlops_agents.proposals.payloads import HandoffPayload

Severity = Literal["critical", "high", "medium", "low"]
IncidentStatus = Literal[
    "open", "acknowledged", "mitigated", "resolved", "closed", "reopened", "merged"
]
ACTIVE_STATUSES: Final[tuple[IncidentStatus, ...]] = (
    "open",
    "acknowledged",
    "mitigated",
    "reopened",
)
ALERT_PRIORITIZATION_AGENT: Final[str] = "alert-prioritization"
SignalKind = Literal[
    "fired_result", "silence", "parser_failure", "source_failure", "notification_failure"
]


class SignalDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: SignalKind
    category: ResultCategory
    subject: str
    model: str | None = None
    ran_at: datetime
    detail: dict[str, Any]
    result_id: str | None = None
    source_ref: str | None = None
    expectation: ExpectationKey | None = None
    cleared: bool = False


class SignalView(SignalDraft):
    id: str
    incident_id: str
    seen_at: datetime
    cause_key: str


class HoldScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tables: list[str] = []
    start: datetime | None = None
    end: datetime | None = None


class IncidentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cause: str | None = None
    category: ResultCategory | None = None
    subtype: str | None = None
    status: Literal["mitigated", "resolved"] | None = None
    note: str | None = None


class SignalOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_id: str
    incident_id: str
    change: Literal["opened", "attached", "reopened", "quiet"]
    severity: Severity


class IncidentChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str
    kind: Literal[
        "opened",
        "severity_raised",
        "severity_lowered",
        "reopened",
        "status",
        "cause",
        "merged",
        "split",
        "hold",
    ]
    before: str | None = None
    after: str | None = None


class IncidentView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: ResultCategory
    subtype: str | None = None
    severity: Severity
    status: IncidentStatus
    cause: str | None = None
    cause_key: str
    model: str | None = None
    signal_ids: list[str] = []
    prs: list[int] = []
    issues: list[int] = []
    held: bool = False
    merged_into: str | None = None
    started_at: datetime
    detected_at: datetime
    acknowledged_at: datetime | None = None
    mitigated_at: datetime | None = None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    snoozed_until: datetime | None = None
    resolved_offline: bool = False
    reopened_count: int = 0
    verdict: VerdictView | None = None


class IncidentObserver(Protocol):
    async def changed(self, project_id: str, change: IncidentChange) -> None: ...


class Incidents(Protocol):
    def add_observer(self, observer: IncidentObserver) -> None: ...

    async def raise_signal(
        self, project_id: str, draft: SignalDraft, seen_at: datetime
    ) -> SignalOutcome: ...

    async def get(self, project_id: str, incident_id: str) -> IncidentView | None: ...

    async def list(
        self,
        project_id: str,
        status: list[IncidentStatus] | None = None,
        inbox: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[IncidentView]: ...

    async def signals(self, project_id: str, incident_id: str) -> list[SignalView]: ...

    async def holds(self, project_id: str, model: str | None) -> list[HoldScope]: ...

    async def acknowledge(self, project_id: str, incident_id: str, by: str) -> IncidentView: ...

    async def snooze(
        self, project_id: str, incident_id: str, until: datetime, by: str
    ) -> IncidentView: ...

    async def close(self, project_id: str, incident_id: str, by: str) -> IncidentView: ...

    async def update(
        self, project_id: str, incident_id: str, agent: str, patch: IncidentPatch
    ) -> IncidentView: ...

    async def merge(self, project_id: str, ids: list[str], agent: str) -> IncidentView: ...

    async def split(
        self, project_id: str, incident_id: str, signal_ids: list[str], agent: str
    ) -> IncidentView: ...

    async def record_hold(
        self, project_id: str, incident_id: str, scope: HoldScope, reason: str, by: str
    ) -> IncidentView: ...

    async def set_severity(
        self, project_id: str, incident_id: str, severity: Severity, reason: str, by: str
    ) -> IncidentView: ...


class SeverityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Severity
    reasons: list[str]


class SeverityPolicy(Protocol):
    async def decide(self, project_id: str, incident: IncidentView) -> SeverityDecision: ...


class MediumSeverity:
    """Contracts default SeverityPolicy returning medium."""

    async def decide(self, project_id: str, incident: IncidentView) -> SeverityDecision:
        return SeverityDecision(severity="medium", reasons=["Default severity"])


class Delivery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person: str
    when: Literal["now", "morning_report"]
    escalation_step: int


class RoutePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deliveries: list[Delivery]
    inbox: bool
    open_issue: bool


class Router(Protocol):
    async def plan(self, project_id: str, incident: IncidentView, now: datetime) -> RoutePlan: ...


WakeLabel = Literal["incident", "fix_request", "post_deploy", "follow_up"]


class AgentWaker(Protocol):
    async def wake(
        self,
        project_id: str,
        agent: str,
        label: WakeLabel,
        subject_id: str,
        text: str,
        key: str,
    ) -> None: ...


def diagnoser(category: ResultCategory, subtype: str | None) -> str:
    """Maps a category and subtype to the packaged diagnosing agent name."""
    if category in ("data_quality", "drift"):
        return "data-quality"
    if category == "model_performance":
        return "model-performance"
    if category == "operational":
        if subtype == "failed_training":
            return "retraining"
        if subtype in ("bad_deploy", "failed_scoring"):
            return "deployment"
        return "orchestrator"
    return "orchestrator"


def cause_key(subject: str, model: str | None) -> str:
    """Builds a collision-resistant cause key for (subject, model)."""
    model_repr = f"m:{model}" if model is not None else "none"
    return f"{len(subject)}:{subject}:{model_repr}"


def stages_of(subject: str, payload: HandoffPayload) -> list[str]:
    """Returns stage names where subject appears in stage.inputs or stage.outputs."""
    return [
        stage.name
        for stage in payload.pipeline
        if subject in stage.inputs or subject in stage.outputs
    ]


class WindowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime
    scope: list[str]
    reason: str


class MaintenancePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: WindowSpec
