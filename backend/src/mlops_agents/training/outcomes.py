"""Fix outcome observer implementation (Task 6, #491)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mlops_agents.monitoring.incidents.contract import IncidentChange, IncidentObserver

if TYPE_CHECKING:
    from mlops_agents.monitoring.incidents.contract import Incidents
    from mlops_agents.training.fixes import SqliteFixes


class FixOutcomeObserver(IncidentObserver):
    """Observes incident lifecycle changes to update fix outcomes."""

    def __init__(self, fixes: SqliteFixes, incidents: Incidents) -> None:
        self._fixes = fixes
        self._incidents = incidents

    async def changed(self, project_id: str, change: IncidentChange) -> None:
        incident = await self._incidents.get(project_id, change.incident_id)
        if incident is None:
            return

        if change.kind == "reopened" or incident.status == "reopened":
            await self._fixes.reopen_incident_fixes(project_id, change.incident_id)
        elif incident.status == "resolved":
            await self._fixes.resolve_incident_fixes(project_id, change.incident_id)
