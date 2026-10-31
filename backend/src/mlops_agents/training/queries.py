"""Query providers for fixes and training requests (Task 5, #490)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mlops_agents.monitoring.query import MonitoringQuery, QueryProvider, QueryWhat
from mlops_agents.monitoring.untrusted import untrusted
from mlops_agents.tracking.contract import Page
from mlops_agents.training.contract import FixRequestView, TrainingRequestView

if TYPE_CHECKING:
    from mlops_agents.training.fixes import SqliteFixes
    from mlops_agents.training.requests import SqliteTraining


class FixesQueryProvider(QueryProvider):
    """Query provider serving what='fixes' via SqliteFixes."""

    what: QueryWhat = "fixes"

    def __init__(self, fixes: SqliteFixes) -> None:
        self._fixes = fixes

    async def query(self, project_id: str, q: MonitoringQuery) -> Page[FixRequestView]:
        incident_id = q.incident_id if q.incident_id is not None else q.subject
        page = await self._fixes.page_fixes(
            project_id,
            fix_id=q.id,
            model=q.model,
            incident_id=incident_id,
            status=q.status,
            since=q.since,
            limit=q.limit,
            offset=q.offset,
        )

        safe_rows: list[FixRequestView] = []
        for row in page.rows:
            evidence = (
                untrusted(row.evidence, source="fix_request") if row.evidence else row.evidence
            )
            why = untrusted(row.why, source="fix_request") if row.why is not None else None
            why_not = {k: untrusted(v, source="fix_request") for k, v in row.why_not.items()}
            safe_rows.append(
                row.model_copy(
                    update={
                        "evidence": evidence,
                        "why": why,
                        "why_not": why_not,
                    }
                )
            )

        return Page(rows=safe_rows, total=page.total, truncated=page.truncated)


class TrainingRequestsQueryProvider(QueryProvider):
    """Query provider serving what='training_requests' via SqliteTraining."""

    what: QueryWhat = "training_requests"

    def __init__(self, training: SqliteTraining) -> None:
        self._training = training

    async def query(self, project_id: str, q: MonitoringQuery) -> Page[TrainingRequestView]:
        return await self._training.page_requests(
            project_id,
            request_id=q.id,
            model=q.model,
            status=q.status,
            since=q.since,
            limit=q.limit,
            offset=q.offset,
        )
