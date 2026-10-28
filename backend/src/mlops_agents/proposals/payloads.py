"""Every payload model of I19: what the Propose tool carries and what the cards show.

All models forbid unknown fields, so nothing (a secret included) rides along unchecked.
References to connections and tracking sources live outside the payload; they are checked when
the proposal is applied. References inside the payload are checked here.
"""

from __future__ import annotations

import re
from collections import Counter
from importlib.resources import files
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from mlops_agents.connections.contract import (
    CONNECTION_NAME_PATTERN,
    LocalFolderKindName,
    LocalFolderSettings,
)
from mlops_agents.proposals.templates import check_template
from mlops_agents.tracking.contract import GithubActionsSettings, MlflowSettings, SourceType

# PermissionLevel minus "bypass": a proposal can never grant it (a test pins the two together).
GrantableLevel = Literal["suggest", "ask", "autonomous"]

Placeholder = Literal[
    "request_id",
    "model",
    "candidate_version",
    "champion_version",
    "window_start",
    "window_end",
    "mode",
    "threshold",
]

_CRON_FIELDS = 5


# The tzdata package's own list, not a lookup in the platform's zone database: Linux's also
# loads posix/..., right/... and posixrules, and a Windows file lookup forgives a trailing space
# or dot and a backslash. Exact membership gives every platform the same names.
_ZONE_NAMES = frozenset(files("tzdata").joinpath("zones").read_text(encoding="utf-8").split())


def _check_timezone(value: str) -> str:
    if value not in _ZONE_NAMES:
        raise ValueError(f"{value!r} is not an IANA time zone name (for example Europe/Paris)")
    return value


def _require_unique(plural: str, names: list[str]) -> None:
    for name in sorted(n for n, count in Counter(names).items() if count > 1):
        raise ValueError(f"two {plural} are named {name!r}")


TimeZoneName = Annotated[str, AfterValidator(_check_timezone)]
ClockTime = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConnectionSpec(_Strict):
    # Settings are typed per kind, so no secret can ride in a free-form dict.
    kind: LocalFolderKindName
    name: Annotated[str, StringConstraints(pattern=CONNECTION_NAME_PATTERN)]
    settings: LocalFolderSettings
    why: str


class ConnectionsPayload(_Strict):
    connections: Annotated[list[ConnectionSpec], Field(min_length=1)]

    @model_validator(mode="after")
    def _names_unique(self) -> ConnectionsPayload:
        _require_unique("connections", [spec.name for spec in self.connections])
        return self


class TableUse(_Strict):
    """A reference to a DataRole."""

    connection: str
    table: str


class DataRole(_Strict):
    connection: str
    table: str
    role: Literal["training", "serving_inputs", "predictions", "labels", "other"]
    id_column: str | None
    time_column: str | None


class GroupSpec(_Strict):
    column: str
    values: list[str] | None


class Profile(_Strict):
    objective: str
    problem_type: str
    target_column: str
    positive_class: str | None
    primary_metric: str
    metric_rationale: str
    operating_threshold: float | None
    label_delay_days: int
    label_maturity_days: int
    serving_mode: Literal["batch", "online", "streaming"]
    error_costs: str
    cost_ratio: float | None
    groups: list[GroupSpec]


class CommandSpec(_Strict):
    kind: Literal["train", "evaluate", "deploy", "rollback", "rerun", "hold", "fall_back"]
    type: Literal["command", "ci_dispatch"]
    template: str | None
    workflow: str | None
    inputs: dict[str, str]
    placeholders: list[Placeholder]
    deploys: bool
    entry_files: list[str]

    @model_validator(mode="after")
    def _check_shape(self) -> CommandSpec:
        # Declared placeholders a command does not use are allowed.
        if self.type == "command":
            if self.template is None or not self.template.strip():
                raise ValueError("a command needs a non-blank template")
            if self.workflow is not None:
                raise ValueError("a command has no workflow (use type ci_dispatch)")
            if self.inputs:
                raise ValueError("a command has no inputs (those belong to a ci_dispatch)")
            check_template("the template", self.template, self.placeholders)
        else:
            if self.workflow is None or not self.workflow.strip():
                raise ValueError("a ci_dispatch needs a non-blank workflow")
            if self.template is not None:
                raise ValueError("a ci_dispatch has no template (use type command)")
        for key, value in sorted(self.inputs.items()):
            check_template(f"the input {key!r}", value, self.placeholders)
        return self


class ProductionSignal(_Strict):
    kind: Literal["tracker_alias", "ci_job", "file"]
    ref: str


class ModelSpec(_Strict):
    name: str
    profile: Profile
    predictions: TableUse | None
    labels: TableUse | None
    commands: list[CommandSpec]
    promotion: Literal["team_gate", "person"]
    environments: dict[str, str]  # tracker alias -> environment
    production_signal: ProductionSignal | None

    @model_validator(mode="after")
    def _check_signal(self) -> ModelSpec:
        signal = self.production_signal
        if (
            signal is not None
            and signal.kind == "tracker_alias"
            and signal.ref not in self.environments
        ):
            raise ValueError(
                f"model {self.name!r}: the production signal alias {signal.ref!r} is not among "
                "its environments"
            )
        return self


class PipelineStage(_Strict):
    name: str
    entry: str
    inputs: list[str]
    outputs: list[str]


class PipelineEdge(_Strict):
    from_stage: str
    to_stage: str


class Cadence(_Strict):
    cron: str | None
    every_minutes: Annotated[int, Field(ge=1)] | None

    @model_validator(mode="after")
    def _exactly_one(self) -> Cadence:
        if (self.cron is None) == (self.every_minutes is None):
            raise ValueError("a cadence sets exactly one of cron and every_minutes")
        if self.cron is not None and len(re.split(r"\s+", self.cron.strip())) != _CRON_FIELDS:
            raise ValueError(
                f"cron {self.cron!r} must have {_CRON_FIELDS} whitespace-separated fields"
            )
        return self


class FileResults(_Strict):
    connection: str
    path_pattern: str


class CiResults(_Strict):
    workflow: str


class MlflowResults(_Strict):
    metric: str
    threshold: float | None
    fires_when: Literal["above", "below"] | None


class DataFeedRef(_Strict):
    connection: str
    table: str


class JobFeedRef(_Strict):
    ci_workflow: str | None
    file: FileResults | None

    @model_validator(mode="after")
    def _exactly_one(self) -> JobFeedRef:
        if (self.ci_workflow is None) == (self.file is None):
            raise ValueError("a job feed sets exactly one of ci_workflow and file")
        return self


class Feed(_Strict):
    name: str
    kind: Literal["data", "job"]
    ref: DataFeedRef | JobFeedRef
    cadence: Cadence
    grace_minutes: int
    timezone: TimeZoneName

    @model_validator(mode="after")
    def _ref_matches_kind(self) -> Feed:
        expected = DataFeedRef if self.kind == "data" else JobFeedRef
        if type(self.ref) is not expected:
            raise ValueError(f"feed {self.name!r}: a {self.kind} feed needs a {expected.__name__}")
        return self


class OnCallHours(_Strict):
    days: Annotated[
        list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]], Field(min_length=1)
    ]
    start: ClockTime
    end: ClockTime

    @model_validator(mode="after")
    def _window(self) -> OnCallHours:
        # An end before the start is a window that crosses midnight; equal ends are ambiguous.
        if self.start == self.end:
            raise ValueError("on-call hours start and end at the same time")
        return self


class Contact(_Strict):
    channel: Literal["discord", "email"]
    address: str


class OnCall(_Strict):
    name: str
    contacts: Annotated[list[Contact], Field(min_length=1)]  # critical alerts go to all
    hours: OnCallHours
    timezone: TimeZoneName
    escalation_order: int


class CheckSpec(_Strict):
    check_id: str
    name: str
    category: Literal["data_quality", "drift", "model_performance", "operational"]
    cadence: Cadence
    timezone: TimeZoneName
    results: FileResults | CiResults | MlflowResults


class Scheduler(_Strict):
    name: str
    config_location: str


class Operations(_Strict):
    scheduler: Scheduler | None
    feeds: list[Feed]
    on_call: list[OnCall]
    checks: list[CheckSpec]
    results_location: FileResults | MlflowResults


class TrackingSourceSpec(_Strict):
    type: SourceType
    settings: MlflowSettings | GithubActionsSettings
    auth: Literal["none", "credential"]

    @model_validator(mode="after")
    def _settings_match_type(self) -> TrackingSourceSpec:
        expected = MlflowSettings if self.type == "mlflow" else GithubActionsSettings
        if type(self.settings) is not expected:
            raise ValueError(f"a {self.type} source needs {expected.__name__} settings")
        return self


class Gap(_Strict):
    what: str
    next_step: str


class Grants(_Strict):
    network: bool
    permission_level: GrantableLevel


class HandoffPayload(_Strict):
    models: list[ModelSpec]
    commands: list[CommandSpec]  # project-level: rerun a data job, hold or quarantine a batch
    data_roles: list[DataRole]
    tracking: list[TrackingSourceSpec]
    pipeline: list[PipelineStage]
    edges: list[PipelineEdge]
    operations: Operations
    gaps: list[Gap]
    grants: Grants

    @model_validator(mode="after")
    def _references_resolve(self) -> HandoffPayload:
        _require_unique("models", [model.name for model in self.models])
        roles = {(role.connection, role.table) for role in self.data_roles}
        for model in self.models:
            for field, use in (("predictions", model.predictions), ("labels", model.labels)):
                if use is not None and (use.connection, use.table) not in roles:
                    raise ValueError(
                        f"model {model.name!r}: {field} table {use.table!r} of connection "
                        f"{use.connection!r} has no data role"
                    )
        _check_pipeline(self.pipeline, self.edges)
        return self


def _check_pipeline(pipeline: list[PipelineStage], edges: list[PipelineEdge]) -> None:
    # Cycles are allowed: real pipelines feed predictions back into training.
    names = [stage.name for stage in pipeline]
    _require_unique("pipeline stages", names)
    seen: set[tuple[str, str]] = set()
    for edge in edges:
        for end in (edge.from_stage, edge.to_stage):
            if end not in names:
                raise ValueError(f"an edge names the unknown pipeline stage {end!r}")
        if edge.from_stage == edge.to_stage:
            raise ValueError(f"an edge joins the pipeline stage {edge.from_stage!r} to itself")
        pair = (edge.from_stage, edge.to_stage)
        if pair in seen:
            raise ValueError(
                f"the edge from {edge.from_stage!r} to {edge.to_stage!r} appears twice"
            )
        seen.add(pair)
