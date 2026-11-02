"""RegisterCheck tool (I33)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar, Final, get_args
from zoneinfo import ZoneInfo

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.tools.base import (
    AgentTool,
    RiskClass,
    ToolContext,
    ToolOutput,
)
from mlops_agents.agents.tools.names import REGISTER_CHECK
from mlops_agents.monitoring.results.contract import ResultCategory, ResultOrigin, ResultRecord
from mlops_agents.monitoring.results.mlflow_source import handoff_metric_user
from mlops_agents.proposals.payloads import Cadence, FileResults, MlflowResults

if TYPE_CHECKING:
    from mlops_agents.monitoring.expectations.checks import ChecksStore
    from mlops_agents.proposals.contract import Handoffs


# The file reader keeps only an agent's records from a results file (file_source.py).
_AGENT_ORIGIN: Final[ResultOrigin] = "agent"
RECORD_EXAMPLE_LABEL: Final = "Example line:"

# What the schema cannot say about a field: the value an agent's check gives it. Keyed by
# ResultRecord's field names; check_record_shape refuses a key out of step with the model.
_RECORD_FIELD_NOTES: Final[dict[str, str]] = {
    "check_id": "the `check_id` registered here",
    "origin": "a line with any other origin is dropped",
    "fired": "true when the check found a problem",
    "value": "the measured value",
    "threshold": "the value it fires against",
    "category": "the category registered here",
    "subject": "what it is about: a table, column, model version or job",
    "model": (
        "for a `model_performance` record, the model's identifier as the incident names it:"
        " confirmation by labels matches records to an incident on it"
    ),
    "detail": "an object with what else the run found ({} when nothing)",
    "source_ref": "where it came from: the check's code in the repository",
    "version": "the git commit that produced the record",
    "not_enough_data": (
        "true when the run's sample is below the check's minimum sample, with `fired` false"
    ),
}

# A field an agent's check always writes with one value, served as that value, not its type.
_WRITTEN_AS: Final[dict[str, str]] = {"origin": _AGENT_ORIGIN}

# One value per ResultRecord field for the served example line, which must validate as one.
_EXAMPLE_VALUES: Final[dict[str, Any]] = {
    "check_id": "label_recall",
    "origin": _AGENT_ORIGIN,
    "ran_at": "2026-11-01T06:00:00Z",
    "fired": False,
    "value": 0.81,
    "threshold": 0.75,
    "category": "model_performance",
    "subject": "churn-model v12",
    "model": "churn-model",
    "detail": {"labels": 1840},
    "source_ref": "checks/label_recall.py",
    "version": "3f9c2e1",
    "not_enough_data": False,
}


def _json_type(prop: dict[str, Any]) -> str:
    if "enum" in prop:
        return "one of " + ", ".join(json.dumps(v) for v in prop["enum"])
    if "anyOf" in prop:
        return " or ".join(_json_type(p) for p in prop["anyOf"])
    kind = prop.get("type")
    if kind not in ("string", "boolean", "number", "integer", "object", "null"):
        raise ValueError(f"No served type for JSON schema {prop!r}")
    fmt = prop.get("format")
    return f"{kind} ({fmt})" if fmt else str(kind)


def check_record_shape() -> str:
    """The record an agent's check writes, generated from ResultRecord so it never drifts."""
    schema = ResultRecord.model_json_schema()
    required = set(schema["required"])
    props: dict[str, dict[str, Any]] = schema["properties"]
    unknown = (set(_RECORD_FIELD_NOTES) | set(_EXAMPLE_VALUES)) ^ set(props)
    if unknown:
        raise ValueError(f"Record notes and example out of step with ResultRecord: {unknown}")
    lines = [
        "A check with a file results location writes each run's records there, one JSON object"
        " per line (JSON Lines), with these fields and no others; a line that does not match is"
        " dropped. A check with an MLflow results location only logs its metric on its run;"
        " the app writes its records.",
    ]
    for name, prop in props.items():
        presence = (
            "required" if name in required else f"optional, default {json.dumps(prop['default'])}"
        )
        kind = json.dumps(_WRITTEN_AS[name]) if name in _WRITTEN_AS else _json_type(prop)
        desc = prop.get("description") or _RECORD_FIELD_NOTES[name]
        lines.append(f"- `{name}` ({kind}, {presence}): {desc}")
    lines += [RECORD_EXAMPLE_LABEL, json.dumps(_EXAMPLE_VALUES)]
    return "\n".join(lines)


CHECK_RECORD_SHAPE: Final = check_record_shape()


class RegisterCheckInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(description="Check identifier (e.g. model_drift)")
    category: ResultCategory = Field(
        description=f"Result category: {', '.join(get_args(ResultCategory))}"
    )
    cadence: Cadence = Field(description="Cadence specification (cron or every_minutes)")
    timezone: str = Field(description="IANA timezone (e.g. UTC, Europe/Paris)")
    results_location: FileResults | MlflowResults = Field(
        description="Results location (file or mlflow)"
    )
    pr_number: int = Field(description="The PR number this check is being introduced in")


class RegisterCheck(AgentTool[RegisterCheckInput]):
    name: ClassVar[str] = REGISTER_CHECK
    description: ClassVar[str] = (
        f"Register a monitoring check pending a pull request merge.\n\n{CHECK_RECORD_SHAPE}"
    )
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = RegisterCheckInput

    def __init__(self, checks: ChecksStore, handoffs: Handoffs) -> None:
        self._checks = checks
        self._handoffs = handoffs

    async def _metric_user(self, project_id: str, check_id: str, metric: str) -> str | None:
        """Who else already reads this MLflow metric, so two checks never share one source.

        The same check registering its metric again is not a conflict, even when the handoff
        names that metric too: the registered check already wins it in the MLflow source.
        """
        others: list[str] = []
        for chk in await self._checks.list(project_id):
            location = chk.results_location
            if (
                chk.status == "retired"
                or not isinstance(location, MlflowResults)
                or location.metric != metric
            ):
                continue
            if chk.check_id == check_id:
                return None
            others.append(f"registered check {chk.check_id!r}")

        user = handoff_metric_user(await self._handoffs.current(project_id), metric)
        if user is not None:
            if user.check is not None:
                return f"handoff check {user.check.check_id!r}"
            return "the handoff's results location"
        return others[0] if others else None

    async def run(self, ctx: ToolContext, args: RegisterCheckInput) -> ToolOutput:
        # Validate timezone
        try:
            ZoneInfo(args.timezone)
        except Exception as exc:
            return ToolOutput(content=f"Malformed timezone {args.timezone!r}: {exc}", is_error=True)

        # Validate cadence
        if args.cadence.cron is not None and not croniter.is_valid(args.cadence.cron):
            return ToolOutput(
                content=f"Malformed cron expression in cadence: {args.cadence.cron!r}",
                is_error=True,
            )

        # Validate results_location
        if isinstance(args.results_location, FileResults):
            if not args.results_location.connection or not args.results_location.path_pattern:
                return ToolOutput(
                    content="Malformed file results location: connection and path_pattern are required.",
                    is_error=True,
                )
        else:
            if not args.results_location.metric:
                return ToolOutput(
                    content="Malformed mlflow results location: metric is required.",
                    is_error=True,
                )

        if isinstance(args.results_location, MlflowResults):
            try:
                user = await self._metric_user(
                    ctx.project_id, args.check_id, args.results_location.metric
                )
            except Exception as exc:
                return ToolOutput(
                    content=f"Failed to check the metric's users: {exc}", is_error=True
                )
            if user is not None:
                return ToolOutput(
                    content=(
                        f"MLflow metric {args.results_location.metric!r} is already used by "
                        f"{user}; one metric feeds one check."
                    ),
                    is_error=True,
                )

        try:
            view = await self._checks.register(
                ctx.project_id,
                check_id=args.check_id,
                category=args.category,
                cadence=args.cadence,
                timezone=args.timezone,
                results_location=args.results_location,
                pr_number=args.pr_number,
                agent=ctx.agent,
            )
        except Exception as exc:
            return ToolOutput(content=f"Failed to register check: {exc}", is_error=True)

        return ToolOutput(
            content=(
                f"Registered check {view.check_id} (ID: {view.id}) with status {view.status}"
                f" pending PR #{view.pr_number}.\n\n{CHECK_RECORD_SHAPE}"
            )
        )
