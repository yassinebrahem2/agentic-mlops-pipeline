"""Tracking sources and their record (I16)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mlops_agents.credentials.scrub import base_url_problem

SourceType = Literal["mlflow", "github_actions"]
# Adapters normalise every tracker's run states to these.
RunStatus = Literal["running", "succeeded", "failed", "cancelled", "other"]
SourceAuth = Literal["none", "credential"]


class MlflowSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        # A credential belongs in the key store, never in the URL (no user:secret@host).
        problem = base_url_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value


class GithubActionsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    workflows: list[str] = []


class TrackingSourceView(BaseModel):
    id: str
    type: SourceType
    settings: dict[str, Any]
    auth: SourceAuth
    credential_set: bool
    last_sync: datetime | None
    last_error: str | None


class ModelVersionRow(BaseModel):
    source_id: str
    model: str
    version: str
    run_key: str | None
    aliases: list[str]
    created: datetime
    # absent: the run has no evaluation.json; fetch_failed: it could not be read (stage 4 then
    # reads the results-location copy).
    evaluation: Literal["present", "absent", "fetch_failed"]


class ModelSummary(BaseModel):
    source_id: str
    name: str
    production_version: str | None
    latest_version: str | None
    versions: int


class RunRow(BaseModel):
    source_id: str
    key: str
    experiment: str
    status: RunStatus
    start: datetime | None
    end: datetime | None
    params: dict[str, str]
    tags: dict[str, str]
    git_commit: str | None
    datasets: list[str]


class MetricPoint(BaseModel):
    source_id: str
    run_key: str
    metric: str
    step: int
    value: float
    time: datetime


class DeploymentRow(BaseModel):
    source_id: str
    model: str
    alias: str
    from_version: str | None
    to_version: str | None  # None: the alias was removed
    seen_at: datetime


class CiRunRow(BaseModel):
    source_id: str
    key: str
    workflow: str
    status: RunStatus
    start: datetime
    duration_s: float | None
    commit: str
    url: str


class EvaluationRecord(BaseModel):
    """A model version run's mlops_agents/evaluation.json (stage 5)."""

    source_id: str
    model: str
    version: str
    run_key: str
    schema_version: int
    payload: dict[str, Any]
    fetched_at: datetime


class SyncResult(BaseModel):
    versions: list[ModelVersionRow]
    runs: list[RunRow]
    metrics: list[MetricPoint]
    # model -> alias -> version; Tracking derives the DeploymentRows from it.
    aliases: dict[str, dict[str, str]]
    ci_runs: list[CiRunRow]
    evaluations: list[EvaluationRecord]
    error: str | None


class Page[T](BaseModel):
    rows: list[T]
    total: int
    truncated: bool


class TrackingQuery(BaseModel):
    what: Literal["models", "versions", "runs", "metrics", "deployments", "ci_runs", "evaluations"]
    model: str | None = None
    version: str | None = None
    source_id: str | None = None
    run_key: str | None = None
    metric: str | None = None
    latest: bool = False
    since: datetime | None = None
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class Tracking(Protocol):
    async def sources(self, project_id: str) -> list[TrackingSourceView]: ...

    async def create_source(
        self, project_id: str, type: SourceType, settings: dict[str, Any], auth: SourceAuth
    ) -> TrackingSourceView: ...

    async def sync(self, project_id: str, source_id: str | None = None) -> None:
        """Starts a sync; the event tracking.synced reports its end."""
        ...

    async def query(self, project_id: str, q: TrackingQuery) -> Page[Any]:
        """Rows typed by `q.what`; TrackingHistory and the routes read it."""
        ...


class TrackingAdapter(Protocol):
    type: SourceType

    async def sync(
        self, source: TrackingSourceView, credential: str | None, since: datetime | None
    ) -> SyncResult: ...
