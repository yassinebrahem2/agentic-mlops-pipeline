"""Incident management tools (I33)."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ulid import ULID

from mlops_agents.agents.tools.base import (
    AgentTool,
    RiskClass,
    ToolContext,
    ToolOutput,
)
from mlops_agents.agents.tools.names import (
    INCIDENT_FOLLOW_UP,
    INCIDENT_MERGE,
    INCIDENT_SPLIT,
    INCIDENT_UPDATE,
)
from mlops_agents.monitoring.incidents.contract import IncidentPatch
from mlops_agents.monitoring.results.contract import ResultCategory
from mlops_agents.providers.failover import Clock, SystemClock
from mlops_agents.storage.tables import incident_follow_ups

if TYPE_CHECKING:
    from mlops_agents.connections.contract import Connections
    from mlops_agents.data.summaries import DataSummaries
    from mlops_agents.monitoring.incidents.contract import Incidents
    from mlops_agents.proposals.contract import Handoffs
    from mlops_agents.storage.databases import Databases


class IncidentUpdateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(description="Incident ID to update.")
    cause: str | None = Field(default=None, description="Updated cause.")
    category: ResultCategory | None = Field(default=None, description="Updated category.")
    subtype: str | None = Field(default=None, description="Updated subtype.")
    status: Literal["mitigated", "resolved"] | None = Field(
        default=None, description="Updated status."
    )
    note: str | None = Field(default=None, description="Note explaining the update.")


class IncidentUpdate(AgentTool[IncidentUpdateArgs]):
    name: ClassVar[str] = INCIDENT_UPDATE
    description: ClassVar[str] = "Update an incident's cause, category, subtype, status, or note."
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = IncidentUpdateArgs

    def __init__(self, incidents: Incidents) -> None:
        self._incidents = incidents

    async def run(self, ctx: ToolContext, args: IncidentUpdateArgs) -> ToolOutput:
        try:
            patch = IncidentPatch(
                cause=args.cause,
                category=args.category,
                subtype=args.subtype,
                status=args.status,
                note=args.note,
            )
            view = await self._incidents.update(ctx.project_id, args.incident_id, ctx.agent, patch)
            return ToolOutput(content=f"Incident {view.id} updated (status={view.status}).")
        except Exception as exc:
            return ToolOutput(content=str(exc), is_error=True)


class IncidentMergeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(description="List of incident IDs to merge.")


class IncidentMerge(AgentTool[IncidentMergeArgs]):
    name: ClassVar[str] = INCIDENT_MERGE
    description: ClassVar[str] = "Merge multiple incidents into one."
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = IncidentMergeArgs

    def __init__(self, incidents: Incidents) -> None:
        self._incidents = incidents

    async def run(self, ctx: ToolContext, args: IncidentMergeArgs) -> ToolOutput:
        try:
            view = await self._incidents.merge(ctx.project_id, args.ids, ctx.agent)
            return ToolOutput(content=f"Merged into incident {view.id}.")
        except Exception as exc:
            return ToolOutput(content=str(exc), is_error=True)


class IncidentSplitArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, description="Incident ID to split.")
    incident_id: str | None = Field(default=None, description="Incident ID to split.")
    signal_ids: list[str] = Field(description="Signal IDs to split into a new incident.")

    @model_validator(mode="after")
    def _check_id(self) -> IncidentSplitArgs:
        if not self.id and not self.incident_id:
            raise ValueError("Either 'id' or 'incident_id' must be provided.")
        return self

    @property
    def target_id(self) -> str:
        return self.incident_id or self.id or ""


class IncidentSplit(AgentTool[IncidentSplitArgs]):
    name: ClassVar[str] = INCIDENT_SPLIT
    description: ClassVar[str] = "Split signals out of an incident into a new incident."
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = IncidentSplitArgs

    def __init__(self, incidents: Incidents) -> None:
        self._incidents = incidents

    async def run(self, ctx: ToolContext, args: IncidentSplitArgs) -> ToolOutput:
        try:
            view = await self._incidents.split(
                ctx.project_id, args.target_id, args.signal_ids, ctx.agent
            )
            return ToolOutput(content=f"Split created incident {view.id}.")
        except Exception as exc:
            return ToolOutput(content=str(exc), is_error=True)


class IncidentFollowUpArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(description="Incident ID to schedule follow-up for.")
    after_days: int | None = Field(default=None, description="Follow up after N days.")
    after_labels: int | None = Field(default=None, description="Follow up after N labels.")
    note: str = Field(description="Note describing the follow-up task.")


class IncidentFollowUp(AgentTool[IncidentFollowUpArgs]):
    name: ClassVar[str] = INCIDENT_FOLLOW_UP
    description: ClassVar[str] = (
        "Schedule a follow-up wake-up for an incident after N days or N labels."
    )
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = IncidentFollowUpArgs

    def __init__(
        self,
        incidents: Incidents,
        databases: Databases,
        handoffs: Handoffs,
        data_summaries: DataSummaries,
        connections: Connections,
        clock: Clock | None = None,
    ) -> None:
        self._incidents = incidents
        self._databases = databases
        self._handoffs = handoffs
        self._data_summaries = data_summaries
        self._connections = connections
        self._clock = clock if clock is not None else SystemClock()

    async def run(self, ctx: ToolContext, args: IncidentFollowUpArgs) -> ToolOutput:
        has_days = args.after_days is not None
        has_labels = args.after_labels is not None
        if (has_days and has_labels) or (not has_days and not has_labels):
            return ToolOutput(
                content="Exactly one of 'after_days' or 'after_labels' must be provided.",
                is_error=True,
            )
        if args.after_days is not None and args.after_days <= 0:
            return ToolOutput(content="'after_days' must be greater than 0.", is_error=True)
        if args.after_labels is not None and args.after_labels <= 0:
            return ToolOutput(content="'after_labels' must be greater than 0.", is_error=True)

        try:
            incident = await self._incidents.get(ctx.project_id, args.incident_id)
            if incident is None:
                return ToolOutput(
                    content=f"Incident '{args.incident_id}' not found.",
                    is_error=True,
                )

            due_iso: str | None = None
            base_count: int | None = None

            if args.after_days is not None:
                now = self._clock.now()
                due_dt = now + timedelta(days=args.after_days)
                due_iso = due_dt.isoformat()
            else:
                if not incident.model:
                    return ToolOutput(
                        content=f"Incident '{args.incident_id}' has no associated model.",
                        is_error=True,
                    )
                handoff = await self._handoffs.current(ctx.project_id)
                if handoff is None:
                    return ToolOutput(content="No approved handoff found.", is_error=True)
                model_spec = next(
                    (m for m in handoff.payload.models if m.name == incident.model), None
                )
                if model_spec is None or model_spec.labels is None:
                    return ToolOutput(
                        content=f"Model '{incident.model}' has no labels table in handoff.",
                        is_error=True,
                    )
                labels_use = model_spec.labels
                refs = await self._connections.tables(ctx.project_id, labels_use.connection)
                ref = next((r for r in refs if r.name == labels_use.table), None)
                if ref is None:
                    return ToolOutput(
                        content=f"Table '{labels_use.table}' not found in connection '{labels_use.connection}'.",
                        is_error=True,
                    )
                profile = await self._data_summaries.profile(
                    ctx.project_id, labels_use.connection, ref
                )
                if profile.row_count is None:
                    return ToolOutput(
                        content=f"Could not determine row count for labels table '{labels_use.table}'.",
                        is_error=True,
                    )
                base_count = profile.row_count

            fol_id = f"fol_{ULID()}"
            engine = await self._databases.project(ctx.project_id)
            async with engine.begin() as conn:
                await conn.execute(
                    incident_follow_ups.insert().values(
                        id=fol_id,
                        incident=args.incident_id,
                        agent=ctx.agent,
                        due=due_iso,
                        after_days=args.after_days,
                        after_labels=args.after_labels,
                        label_base_count=base_count,
                        note=args.note,
                        done=0,
                    )
                )
            return ToolOutput(
                content=f"Follow-up scheduled for incident {args.incident_id} (id={fol_id})."
            )
        except Exception as exc:
            return ToolOutput(content=str(exc), is_error=True)
