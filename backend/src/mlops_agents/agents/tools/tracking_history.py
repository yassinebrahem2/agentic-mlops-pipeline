"""TrackingHistory tool: query the project's tracking record (I16)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.tools.base import (
    AgentTool,
    RiskClass,
    ToolContext,
    ToolFailure,
    ToolOutput,
)
from mlops_agents.errors import DomainError
from mlops_agents.tracking.constants import QUERY_LIMIT_DEFAULT, QUERY_LIMIT_MAX
from mlops_agents.tracking.contract import (
    CiRunRow,
    DeploymentRow,
    EvaluationRecord,
    MetricPoint,
    ModelSummary,
    ModelVersionRow,
    RunRow,
    TrackingQuery,
)

if TYPE_CHECKING:
    from mlops_agents.tracking.contract import Tracking

TRACKING_HISTORY_MAX_CHARS: Final[int] = 8000


def _cut(value: Any, max_chars: int = 80) -> str:
    text = str(value).replace("\r", "").replace("\n", " ") if value is not None else "-"
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def _format_row(row: object) -> str:
    if isinstance(row, ModelSummary):
        return (
            f"model: {_cut(row.name)} | "
            f"source: {_cut(row.source_id)} | "
            f"prod: {_cut(row.production_version)} | "
            f"latest: {_cut(row.latest_version)} | "
            f"versions: {row.versions}"
        )
    if isinstance(row, ModelVersionRow):
        aliases_str = ",".join(row.aliases) if row.aliases else "-"
        return (
            f"model: {_cut(row.model)} | "
            f"version: {_cut(row.version)} | "
            f"source: {_cut(row.source_id)} | "
            f"run: {_cut(row.run_key)} | "
            f"aliases: {_cut(aliases_str)} | "
            f"eval: {_cut(row.evaluation)} | "
            f"created: {_cut(row.created.isoformat())}"
        )
    if isinstance(row, RunRow):
        start_str = row.start.isoformat() if row.start else "-"
        return (
            f"run: {_cut(row.key)} | "
            f"status: {_cut(row.status)} | "
            f"source: {_cut(row.source_id)} | "
            f"exp: {_cut(row.experiment)} | "
            f"start: {_cut(start_str)} | "
            f"commit: {_cut(row.git_commit)}"
        )
    if isinstance(row, MetricPoint):
        return (
            f"run: {_cut(row.run_key)} | "
            f"metric: {_cut(row.metric)} | "
            f"step: {row.step} | "
            f"value: {row.value} | "
            f"time: {_cut(row.time.isoformat())} | "
            f"source: {_cut(row.source_id)}"
        )
    if isinstance(row, DeploymentRow):
        return (
            f"model: {_cut(row.model)} | "
            f"alias: {_cut(row.alias)} | "
            f"from: {_cut(row.from_version)} -> to: {_cut(row.to_version)} | "
            f"seen: {_cut(row.seen_at.isoformat())} | "
            f"source: {_cut(row.source_id)}"
        )
    if isinstance(row, CiRunRow):
        dur = f"{row.duration_s}s" if row.duration_s is not None else "-"
        return (
            f"run: {_cut(row.key)} | "
            f"workflow: {_cut(row.workflow)} | "
            f"status: {_cut(row.status)} | "
            f"start: {_cut(row.start.isoformat())} | "
            f"duration: {dur} | "
            f"commit: {_cut(row.commit)}"
        )
    if isinstance(row, EvaluationRecord):
        payload_compact = json.dumps(row.payload, separators=(",", ":"))
        if len(payload_compact) > 400:
            payload_compact = payload_compact[:397] + "..."
        return (
            f"model: {_cut(row.model)} | "
            f"version: {_cut(row.version)} | "
            f"run: {_cut(row.run_key)} | "
            f"schema: {row.schema_version} | "
            f"fetched: {_cut(row.fetched_at.isoformat())} | "
            f"payload: {payload_compact}"
        )
    return str(row)


class TrackingHistoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what: Literal[
        "models", "versions", "runs", "metrics", "deployments", "ci_runs", "evaluations"
    ] = Field(description="The kind of tracking entity to query.")
    model: str | None = Field(default=None, description="Filter by model name.")
    version: str | None = Field(default=None, description="Filter by version string.")
    source_id: str | None = Field(default=None, description="Filter by tracking source ID.")
    run_key: str | None = Field(default=None, description="Filter by run key.")
    metric: str | None = Field(default=None, description="Filter by metric name (metrics query).")
    latest: bool | None = Field(
        default=None, description="When true, return only latest metric values."
    )
    since: datetime | None = Field(
        default=None,
        description="Filter entities created/started/seen at or after this UTC timestamp.",
    )
    limit: int = Field(
        default=QUERY_LIMIT_DEFAULT,
        ge=1,
        description=f"Maximum number of rows to return (default {QUERY_LIMIT_DEFAULT}, max {QUERY_LIMIT_MAX}).",
    )
    offset: int = Field(default=0, ge=0, description="Number of rows to skip.")


class TrackingHistory(AgentTool[TrackingHistoryInput]):
    name: ClassVar[str] = "TrackingHistory"
    description: ClassVar[str] = (
        "Query the project's tracking history for models, versions, runs, metrics, "
        "deployments, ci-runs, or evaluations."
    )
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = TrackingHistoryInput

    def __init__(self, tracking: Tracking | None = None) -> None:
        self._tracking = tracking

    async def run(self, ctx: ToolContext, args: TrackingHistoryInput) -> ToolOutput:
        if self._tracking is None:
            raise ToolFailure("Tracking is not configured for this project.")

        clamped_limit = min(args.limit, QUERY_LIMIT_MAX)
        query = TrackingQuery(
            what=args.what,
            model=args.model,
            version=args.version,
            source_id=args.source_id,
            run_key=args.run_key,
            metric=args.metric,
            latest=bool(args.latest),
            since=args.since,
            limit=clamped_limit,
            offset=args.offset,
        )

        try:
            page = await self._tracking.query(ctx.project_id, query)
        except DomainError as err:
            raise ToolFailure(err.message) from None

        if not page.rows:
            try:
                sources = await self._tracking.sources(ctx.project_id)
                source_errors = [
                    f"Source {s.id} error: {s.last_error}" for s in sources if s.last_error
                ]
            except Exception:
                source_errors = []
            if source_errors:
                return ToolOutput(f"No {args.what} recorded yet.\n" + "\n".join(source_errors))
            return ToolOutput(f"No {args.what} recorded yet.")

        lines = [_format_row(row) for row in page.rows]
        lines.append(f"{len(page.rows)} of {page.total} rows")
        if page.truncated:
            lines.append(
                "Results are truncated. Narrow the query using filters such as 'since', "
                "'model', 'limit', or 'offset'."
            )
        output = "\n".join(lines)
        if len(output) > TRACKING_HISTORY_MAX_CHARS:
            trunc_notice = "\n[... truncated ...]"
            output = output[: TRACKING_HISTORY_MAX_CHARS - len(trunc_notice)] + trunc_notice
        return ToolOutput(output)
