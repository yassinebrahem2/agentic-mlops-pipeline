"""Every payload model of I19: what the Propose tool carries and what the cards show.

All models forbid unknown fields, so nothing (a secret included) rides along unchecked.
References to connections and tracking sources live outside the payload; they are checked when
the proposal is applied. References inside the payload are checked here.
"""

from __future__ import annotations

import re
from string import Formatter
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


# ZoneInfo hands the name to the file system, which on Windows forgives a trailing space or dot
# and a backslash; only the database's own spelling is accepted.
_IANA_NAME = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*")


def _check_timezone(value: str) -> str:
    if _IANA_NAME.fullmatch(value) is None:
        raise ValueError(f"{value!r} is not an IANA time zone name (for example Europe/Paris)")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError) as err:
        # ValueError: a malformed key such as "" or a path escape. OSError: a region folder such
        # as "Europe" is a directory in the zone database, which some platforms report as one.
        raise ValueError(
            f"{value!r} is not an IANA time zone name (for example Europe/Paris)"
        ) from err
    return value


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
    connections: list[ConnectionSpec]


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
        if self.type == "command":
            if not self.template:
                raise ValueError("a command needs a template")
            if self.workflow is not None:
                raise ValueError("a command has no workflow (use type ci_dispatch)")
        elif not self.workflow:
            raise ValueError("a ci_dispatch needs a workflow")
        if self.template is not None:
            # Declared-but-unused placeholders are fine: ci_dispatch inputs use them.
            used = {name for _, name, _, _ in Formatter().parse(self.template) if name is not None}
            for name in sorted(used - set(self.placeholders)):
                raise ValueError(
                    f"the template uses {{{name}}}, which the command does not declare"
                )
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


class OnCallHours(_Strict):
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]]
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
        names = [model.name for model in self.models]
        for name in sorted({n for n in names if names.count(n) > 1}):
            raise ValueError(f"two models are named {name!r}")
        roles = {(role.connection, role.table) for role in self.data_roles}
        for model in self.models:
            for field, use in (("predictions", model.predictions), ("labels", model.labels)):
                if use is not None and (use.connection, use.table) not in roles:
                    raise ValueError(
                        f"model {model.name!r}: {field} table {use.table!r} of connection "
                        f"{use.connection!r} has no data role"
                    )
        stages = {stage.name for stage in self.pipeline}
        for edge in self.edges:
            for end in (edge.from_stage, edge.to_stage):
                if end not in stages:
                    raise ValueError(f"an edge names the unknown pipeline stage {end!r}")
        return self
