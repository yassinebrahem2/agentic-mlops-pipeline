"""The Dashboard's view of one project (I39), assembled from the stores that own each part."""

from __future__ import annotations

from typing import Any, Protocol, get_args

from mlops_agents.config import RunMode
from mlops_agents.events.contract import AgentStatus
from mlops_agents.monitoring.contract import (
    AgentActivity,
    DashboardView,
    ModelCard,
    StageStatus,
)
from mlops_agents.monitoring.expectations.contract import ExpectationState
from mlops_agents.monitoring.incidents.contract import (
    ACTIVE_STATUSES,
    IncidentStatus,
    IncidentView,
    Severity,
    SignalView,
    stages_of,
)
from mlops_agents.monitoring.results.contract import ResultQuery, StoredResult
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import HandoffPayload
from mlops_agents.tracking.contract import ModelSummary, Page, TrackingQuery

# One page of incidents or results per read, so a project with many never loads them in one go.
_PAGE = 200
_AT_RISK = ("due", "late", "silent")
_LATE = ("late", "silent")


class _HandoffReader(Protocol):
    async def current(self, project_id: str) -> HandoffView | None: ...


class _TrackingReader(Protocol):
    async def query(self, project_id: str, q: TrackingQuery) -> Page[Any]: ...


class _IncidentReader(Protocol):
    async def list(
        self,
        project_id: str,
        status: list[IncidentStatus] | None = None,
        inbox: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[IncidentView]: ...

    async def signals(self, project_id: str, incident_id: str) -> list[SignalView]: ...


class _ExpectationReader(Protocol):
    async def states(self, project_id: str) -> list[ExpectationState]: ...


class _ResultReader(Protocol):
    async def query(self, project_id: str, q: ResultQuery) -> Page[StoredResult]: ...


class _BaselineReader(Protocol):
    async def available(self, project_id: str) -> bool: ...


class _StatusReader(Protocol):
    async def snapshot(self, project_id: str) -> list[AgentStatus]: ...


class DashboardBuilder:
    def __init__(
        self,
        *,
        handoffs: _HandoffReader,
        tracking: _TrackingReader,
        incidents: _IncidentReader,
        expectations: _ExpectationReader,
        results: _ResultReader,
        baseline: _BaselineReader,
        status_board: _StatusReader,
        mode: RunMode,
    ) -> None:
        self._handoffs = handoffs
        self._tracking = tracking
        self._incidents = incidents
        self._expectations = expectations
        self._results = results
        self._baseline = baseline
        self._status_board = status_board
        self._mode = mode

    async def build(self, project_id: str) -> DashboardView:
        handoff = await self._handoffs.current(project_id)
        payload = handoff.payload if handoff is not None else None
        active = await self._active_incidents(project_id)
        states = await self._expectations.states(project_id)
        return DashboardView(
            stages=await self._stages(project_id, payload, active, states),
            edges=payload.edges if payload is not None else [],
            models=await self._models(project_id, payload, active),
            incidents_by_severity=_by_severity(active),
            at_risk=[s for s in states if s.status in _AT_RISK],
            agents=[
                AgentActivity(
                    agent=s.agent, state=s.state, doing=s.task.title if s.task is not None else None
                )
                for s in await self._status_board.snapshot(project_id)
            ],
            heartbeat_reachable=self._mode is RunMode.SERVER,
            baseline_available=await self._baseline.available(project_id),
        )

    async def _active_incidents(self, project_id: str) -> list[IncidentView]:
        found: list[IncidentView] = []
        while True:
            page = await self._incidents.list(
                project_id, status=list(ACTIVE_STATUSES), limit=_PAGE, offset=len(found)
            )
            found.extend(page.rows)
            if not page.truncated or not page.rows:
                return found

    async def _stages(
        self,
        project_id: str,
        payload: HandoffPayload | None,
        active: list[IncidentView],
        states: list[ExpectationState],
    ) -> list[StageStatus]:
        if payload is None:
            return []
        incident_ids: dict[str, list[str]] = {}
        for incident in active:
            affected: set[str] = set()
            for signal in await self._incidents.signals(project_id, incident.id):
                affected.update(stages_of(signal.subject, payload))
            for stage in affected:
                incident_ids.setdefault(stage, []).append(incident.id)
        late: set[str] = set()
        with_expectation: set[str] = set()
        for state in states:
            # The signal a silent or late expectation raises carries its ref as subject, so
            # the ref is matched the way incident grouping matches a signal.
            belongs = set(stages_of(state.key.ref, payload))
            with_expectation |= belongs
            if state.status in _LATE:
                late |= belongs
        stages: list[StageStatus] = []
        for stage in payload.pipeline:
            ids = incident_ids.get(stage.name, [])
            if ids:
                status = "incident"
            elif stage.name in late:
                status = "late"
            elif stage.name in with_expectation or await self._has_results(
                project_id, [*stage.inputs, *stage.outputs]
            ):
                status = "ok"
            else:
                status = "unknown"
            stages.append(StageStatus(name=stage.name, status=status, incident_ids=ids))
        return stages

    async def _has_results(self, project_id: str, subjects: list[str]) -> bool:
        for subject in subjects:
            page = await self._results.query(project_id, ResultQuery(subject=subject, limit=1))
            if page.total > 0:
                return True
        return False

    async def _models(
        self, project_id: str, payload: HandoffPayload | None, active: list[IncidentView]
    ) -> list[ModelCard]:
        if payload is None:
            return []
        production = await self._production_versions(project_id)
        return [
            ModelCard(
                name=spec.name,
                production_version=production.get(spec.name),
                latest_metrics=await self._latest_metrics(project_id, spec.name),
                open_incidents=sum(1 for i in active if i.model == spec.name),
            )
            for spec in payload.models
        ]

    async def _production_versions(self, project_id: str) -> dict[str, str]:
        versions: dict[str, str] = {}
        offset = 0
        while True:
            page = await self._tracking.query(
                project_id, TrackingQuery(what="models", limit=_PAGE, offset=offset)
            )
            for row in page.rows:
                summary = ModelSummary.model_validate(row)
                # The same model name can be tracked by several sources; the first with a
                # production version speaks for it.
                if summary.production_version is not None:
                    versions.setdefault(summary.name, summary.production_version)
            offset += len(page.rows)
            if not page.truncated or not page.rows:
                return versions

    async def _latest_metrics(self, project_id: str, model: str) -> dict[str, float]:
        metrics: dict[str, float] = {}
        offset = 0
        while True:
            page = await self._results.query(
                project_id, ResultQuery(model=model, latest=True, limit=_PAGE, offset=offset)
            )
            for record in page.rows:
                if record.value is not None:
                    metrics[record.check_id] = record.value
            offset += len(page.rows)
            if not page.truncated or not page.rows:
                return metrics


def _by_severity(active: list[IncidentView]) -> dict[Severity, int]:
    # Built from the Severity type, so every level is present, zero included, in its order.
    counts: dict[Severity, int] = {level: 0 for level in get_args(Severity)}
    for incident in active:
        counts[incident.severity] += 1
    return counts
