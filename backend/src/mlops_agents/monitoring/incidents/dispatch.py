"""Diagnosis dispatch observer: wakes the appropriate diagnosing agent on incident changes."""

from __future__ import annotations

import json

from mlops_agents.monitoring.incidents.contract import (
    AgentWaker,
    IncidentChange,
    IncidentObserver,
    Incidents,
    diagnoser,
)
from mlops_agents.monitoring.untrusted import untrusted


class DiagnosisDispatch(IncidentObserver):
    """IncidentObserver that dispatches diagnosing agents based on incident category and subtype."""

    def __init__(self, incidents: Incidents, waker: AgentWaker) -> None:
        self._incidents = incidents
        self._waker = waker
        self._current_diagnoser: dict[tuple[str, str], str] = {}
        self._cause_woken: set[tuple[str, str]] = set()

    async def changed(self, project_id: str, change: IncidentChange) -> None:
        incident = await self._incidents.get(project_id, change.incident_id)
        if incident is None or incident.resolved_offline:
            return

        incident_key = (project_id, incident.id)
        target_agent: str
        key: str

        if change.kind == "opened":
            target_agent = diagnoser(incident.category, incident.subtype)
            key = "opened"
            self._current_diagnoser[incident_key] = target_agent
            self._cause_woken.discard(incident_key)
        elif change.kind == "reopened":
            target_agent = diagnoser(incident.category, incident.subtype)
            key = f"reopened:{incident.reopened_count}"
            self._current_diagnoser[incident_key] = target_agent
            self._cause_woken.discard(incident_key)
        elif change.kind == "cause":
            new_agent = diagnoser(incident.category, incident.subtype)
            prev_agent = self._current_diagnoser.get(incident_key)
            if prev_agent is None:
                sigs = await self._incidents.signals(project_id, incident.id)
                prev_agent = diagnoser(sigs[0].category, None) if sigs else new_agent

            if new_agent == prev_agent:
                self._cause_woken.discard(incident_key)
                return

            target_agent = new_agent
            key = f"diagnoser:{new_agent}"
            self._current_diagnoser[incident_key] = new_agent
            self._cause_woken.add(incident_key)
        elif change.kind == "severity_raised":
            if incident_key in self._cause_woken:
                self._cause_woken.discard(incident_key)
                return
            target_agent = diagnoser(incident.category, incident.subtype)
            severity_after = change.after or incident.severity
            key = f"severity:{severity_after}"
        else:
            self._cause_woken.discard(incident_key)
            return

        signals = await self._incidents.signals(project_id, incident.id)

        lines: list[str] = [
            f"Incident {incident.id} with severity {incident.severity} requires diagnosis.",
            f"Category: {incident.category}",
        ]
        if incident.subtype:
            lines.append(f"Subtype: {incident.subtype}")
        if incident.cause:
            lines.append("Cause:")
            lines.append(untrusted(incident.cause, source="incident"))

        for sig in signals:
            lines.append("Signal subject:")
            lines.append(untrusted(sig.subject, source="incident"))
            if sig.detail:
                detail_str = (
                    sig.detail
                    if isinstance(sig.detail, str)
                    else json.dumps(sig.detail, sort_keys=True)
                )
                lines.append("Signal detail:")
                lines.append(untrusted(detail_str, source="incident"))

        text = "\n".join(lines)

        await self._waker.wake(
            project_id=project_id,
            agent=target_agent,
            label="incident",
            subject_id=incident.id,
            text=text,
            key=key,
        )
