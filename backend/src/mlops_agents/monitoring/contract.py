"""Dashboard contracts and types (I39)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from mlops_agents.events.contract import AgentState
from mlops_agents.monitoring.expectations.contract import ExpectationState
from mlops_agents.monitoring.incidents.contract import Severity
from mlops_agents.proposals.payloads import PipelineEdge


class StageStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: Literal["ok", "late", "incident", "unknown"]
    incident_ids: list[str] = []


class ModelCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    production_version: str | None = None
    latest_metrics: dict[str, float] = {}
    open_incidents: int = 0


class AgentActivity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str
    state: AgentState
    doing: str | None = None


class DashboardView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stages: list[StageStatus]
    edges: list[PipelineEdge]
    models: list[ModelCard]
    incidents_by_severity: dict[Severity, int]
    at_risk: list[ExpectationState]
    agents: list[AgentActivity]
    heartbeat_reachable: bool
    baseline_available: bool
