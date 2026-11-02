"""Results contracts, records and protocols (I22)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mlops_agents.monitoring.expr import Condition
from mlops_agents.tracking.contract import EvaluationRecord, Page

ResultCategory = Literal["data_quality", "drift", "model_performance", "operational"]
ResultOrigin = Literal["team", "agent", "tracker"]
SourceKind = Literal["file", "ci", "mlflow"]


class ResultRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str
    origin: ResultOrigin
    ran_at: datetime = Field(
        description=(
            "when the check ran (ISO 8601 with its timezone); the date of the data it covers goes"
            " in `detail`, never here: the app judges silence by this time"
        )
    )
    fired: bool
    value: float | None = None
    threshold: float | None = None
    category: ResultCategory
    subject: str
    model: str | None = None
    detail: dict[str, Any]
    source_ref: str
    version: str | None = None
    not_enough_data: bool = False


class StoredResult(ResultRecord):
    id: str
    source_id: str
    source_key: str
    seen_at: datetime


class ResultQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[str] | None = None
    check_id: str | None = None
    model: str | None = None
    origin: ResultOrigin | None = None
    subject: str | None = None
    fired: bool | None = None
    since: datetime | None = None
    until: datetime | None = None
    latest: bool = False
    limit: int = Field(default=50, le=500)
    offset: int = 0


class SourceState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    kind: SourceKind
    cursor: dict[str, Any]
    last_read: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int


class CollectionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stored: int
    by_source: dict[str, int]
    failures: dict[str, str]


def source_id(kind: SourceKind, ref: str, connection: str | None = None) -> str:
    """Builds a canonical source_id for file, ci, or mlflow.

    `connection` is required for 'file' and refused for 'ci' and 'mlflow'.
    """
    if kind == "file":
        if not connection:
            raise ValueError("connection is required for file source_id")
        return f"file:{connection}:{ref}"
    if connection is not None:
        raise ValueError(f"connection is not allowed for {kind} source_id")
    return f"{kind}:{ref}"


def parse_source_id(source_id: str) -> tuple[SourceKind, str, str | None]:
    """Inverse of source_id: returns (kind, ref, connection_name | None)."""
    parts = source_id.split(":", 2)
    if len(parts) < 2:
        raise ValueError(f"Invalid source_id {source_id!r}")
    kind_str = parts[0]
    if kind_str not in ("file", "ci", "mlflow"):
        raise ValueError(f"Unknown source kind {kind_str!r} in {source_id!r}")
    kind: SourceKind = kind_str
    if kind == "file":
        if len(parts) != 3:
            raise ValueError(
                f"Invalid file source_id (expected file:<connection>:<pattern>): {source_id!r}"
            )
        return "file", parts[2], parts[1]
    return kind, parts[1], None


class ResultsObserver(Protocol):
    async def stored(self, project_id: str, results: Sequence[StoredResult]) -> None: ...


class Results(Protocol):
    def add_observer(self, observer: ResultsObserver) -> None: ...

    async def ingest(
        self,
        project_id: str,
        source_id: str,
        items: Sequence[tuple[str, ResultRecord]],
        seen_at: datetime,
    ) -> list[StoredResult]: ...

    async def collect(self, project_id: str, now: datetime) -> CollectionReport: ...

    async def query(self, project_id: str, q: ResultQuery) -> Page[StoredResult]: ...

    async def latest_per_check(
        self, project_id: str, subject: str, model: str | None
    ) -> list[StoredResult]: ...


class ParserMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["jsonl", "json", "csv", "log"]
    fields: dict[str, str]
    pattern: str | None = None
    fired_when: Condition
    defaults: dict[str, Any] = {}


class ParserCode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script: str
    timeout_s: int = 30


class ParserSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    mapping: ParserMapping | None = None
    code: ParserCode | None = None

    @model_validator(mode="after")
    def _validate_spec(self) -> ParserSpec:
        if (self.mapping is None and self.code is None) or (
            self.mapping is not None and self.code is not None
        ):
            raise ValueError("ParserSpec must specify exactly one of mapping or code")
        return self


class ParserVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    version: int
    spec: ParserSpec
    proposal_id: str
    approved: datetime
    enabled: bool
    consecutive_failures: int


class ParseOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[tuple[str, ResultRecord]]
    errors: list[str]


class ParserPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: ParserSpec
    sample_input: str
    sample_records: list[ResultRecord]


class Parsers(Protocol):
    async def store(self, project_id: str, spec: ParserSpec, proposal_id: str) -> ParserVersion: ...

    async def current(self, project_id: str, source_id: str) -> ParserVersion | None: ...

    async def parse(
        self,
        project_id: str,
        source_id: str,
        raw: str,
        origin_ref: str,
        *,
        start_offset: int = 0,
        csv_header: list[str] | None = None,
    ) -> ParseOutcome: ...


class EvaluationCopies(Protocol):
    async def store_copy(self, project_id: str, record: EvaluationRecord) -> bool: ...
