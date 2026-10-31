"""The baseline's two observers (I31): they link team fires to incidents from either side.

Both sit on paths that must not fail: the incident observer is awaited inside `raise_signal`
(paging), the results observer beside incidents' own observer during delivery. Each catches
its own errors and logs them at warning, so a baseline bug never delays a page or an incident.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from mlops_agents.monitoring.incidents.contract import IncidentChange, IncidentObserver
from mlops_agents.monitoring.results.contract import ResultsObserver, StoredResult
from mlops_agents.monitoring.verdicts.baseline import SqliteBaseline

logger = logging.getLogger(__name__)


class BaselineResultsObserver(ResultsObserver):
    """Links newly stored team fires; its own failure never fails the delivery."""

    def __init__(self, baseline: SqliteBaseline) -> None:
        self._baseline = baseline

    async def stored(self, project_id: str, results: Sequence[StoredResult]) -> None:
        try:
            await self._baseline.link_fires(project_id, results)
        except Exception:
            logger.warning(
                "Baseline links failed for results %s in project %s",
                ", ".join(r.id for r in results),
                project_id,
                exc_info=True,
            )


class BaselineIncidentObserver(IncidentObserver):
    """On `opened` and `reopened`, looks back for team fires already stored; its own failure
    never fails `raise_signal`, which awaits it while paging."""

    def __init__(self, baseline: SqliteBaseline) -> None:
        self._baseline = baseline

    async def changed(self, project_id: str, change: IncidentChange) -> None:
        if change.kind not in ("opened", "reopened"):
            return
        try:
            await self._baseline.look_back(project_id, change.incident_id)
        except Exception:
            logger.warning(
                "Baseline look-back failed for incident %s in project %s",
                change.incident_id,
                project_id,
                exc_info=True,
            )
