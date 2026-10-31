"""Verdicts query provider (I33)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mlops_agents.monitoring.query import MonitoringQuery, QueryWhat
from mlops_agents.monitoring.verdicts.contract import VerdictView
from mlops_agents.tracking.contract import Page

if TYPE_CHECKING:
    from mlops_agents.monitoring.verdicts.store import SqliteVerdicts


class VerdictsQueryProvider:
    """Verdicts by `incident_id` and `since` (on `created`), newest first.

    A verdict's `note` is a person's free text: the MonitoringQuery tool shows these rows to an
    agent only inside its untrusted block.
    """

    what: QueryWhat = "verdicts"

    def __init__(self, verdicts: SqliteVerdicts) -> None:
        self._verdicts = verdicts

    async def query(self, project_id: str, q: MonitoringQuery) -> Page[VerdictView]:
        return await self._verdicts.page(
            project_id, incident_id=q.incident_id, since=q.since, limit=q.limit, offset=q.offset
        )
