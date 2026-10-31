"""On-call router implementation (S4 §7)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from mlops_agents.agents.tools.issues import PRIORITY_RANKS, get_issue_min_priority
from mlops_agents.monitoring.incidents.contract import Delivery, IncidentView, RoutePlan
from mlops_agents.monitoring.scheduler.times import in_hours

if TYPE_CHECKING:
    from mlops_agents.notifications.contract import OnCallPeople
    from mlops_agents.storage.databases import Databases


class OnCallRouter:
    """Routes incidents to on-call people, the morning report, and GitHub issues."""

    def __init__(self, on_call: OnCallPeople, databases: Databases) -> None:
        self._on_call = on_call
        self._databases = databases

    async def plan(self, project_id: str, incident: IncidentView, now: datetime) -> RoutePlan:
        people = await self._on_call.list(project_id)
        sorted_people = sorted(people, key=lambda p: (p.escalation_order, p.id))

        deliveries: list[Delivery] = []
        inbox = True

        if incident.severity == "critical":
            in_hours_person = next(
                (p for p in sorted_people if in_hours(p.hours, p.timezone, now)),
                None,
            )
            if in_hours_person is not None:
                deliveries = [Delivery(person=in_hours_person.id, when="now", escalation_step=0)]
            elif sorted_people:
                deliveries = [Delivery(person=sorted_people[0].id, when="now", escalation_step=0)]
        elif incident.severity == "high":
            in_hours_person = next(
                (p for p in sorted_people if in_hours(p.hours, p.timezone, now)),
                None,
            )
            if in_hours_person is not None:
                deliveries = [Delivery(person=in_hours_person.id, when="now", escalation_step=0)]
            elif sorted_people:
                deliveries = [
                    Delivery(person=sorted_people[0].id, when="morning_report", escalation_step=0)
                ]
        else:
            # Medium and low: inbox only
            deliveries = []

        min_priority = await get_issue_min_priority(self._databases, project_id)
        open_issue = PRIORITY_RANKS.get(incident.severity, 0) >= PRIORITY_RANKS.get(min_priority, 2)

        return RoutePlan(deliveries=deliveries, inbox=inbox, open_issue=open_issue)
