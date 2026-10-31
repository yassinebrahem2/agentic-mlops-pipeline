"""Prioritization wake observer: wakes alert-prioritization on incident changes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from mlops_agents.events.contract import EventType
from mlops_agents.monitoring.incidents.contract import IncidentChange, IncidentObserver

if TYPE_CHECKING:
    from mlops_agents.events.contract import EventLog
    from mlops_agents.monitoring.incidents.contract import AgentWaker

ALERT_PRIORITIZATION_AGENT: Final[str] = "alert-prioritization"
WAKE_LABEL: Final[str] = "incident"
SEVERITY_CHANGED_EVENT: Final[EventType] = "incident.severity_changed"


class PrioritizationWake(IncidentObserver):
    """IncidentObserver that wakes alert-prioritization on relevant incident changes."""

    def __init__(self, waker: AgentWaker, events: EventLog) -> None:
        self._waker = waker
        self._events = events

    async def changed(self, project_id: str, change: IncidentChange) -> None:
        if change.kind in ("severity_raised", "severity_lowered"):
            events = await self._events.page(
                project_id,
                subject_id=change.incident_id,
                types=[SEVERITY_CHANGED_EVENT],
                limit=1,
            )
            if events and events[0].source == ALERT_PRIORITIZATION_AGENT:
                return
        elif change.kind not in ("opened", "reopened", "merged", "split"):
            return

        key = f"{change.kind}:{change.incident_id}"
        text = (
            f"Incident {change.incident_id} changed ({change.kind}). "
            "Please review and adjust alert prioritization."
        )

        await self._waker.wake(
            project_id=project_id,
            agent=ALERT_PRIORITIZATION_AGENT,
            label=WAKE_LABEL,
            subject_id=change.incident_id,
            text=text,
            key=key,
        )
