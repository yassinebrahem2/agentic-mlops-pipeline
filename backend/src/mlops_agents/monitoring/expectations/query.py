"""Query providers for results and expectations (I33)."""

from __future__ import annotations

from typing import Protocol

from mlops_agents.monitoring.expectations.contract import ExpectationState
from mlops_agents.monitoring.query import MonitoringQuery, QueryProvider, QueryWhat
from mlops_agents.monitoring.results.contract import ResultQuery, StoredResult
from mlops_agents.tracking.contract import Page


class ResultsProtocol(Protocol):
    async def query(self, project_id: str, q: ResultQuery) -> Page[StoredResult]: ...


class ExpectationsProtocol(Protocol):
    async def states(self, project_id: str) -> list[ExpectationState]: ...


class ResultsQueryProvider(QueryProvider):
    """Query provider serving what='results' via Results.query."""

    what: QueryWhat = "results"

    def __init__(self, results: ResultsProtocol) -> None:
        self._results = results

    async def query(self, project_id: str, q: MonitoringQuery) -> Page[StoredResult]:
        ids = [q.id] if q.id is not None else None
        res_query = ResultQuery(
            ids=ids,
            model=q.model,
            subject=q.subject,
            since=q.since,
            limit=q.limit,
            offset=q.offset,
        )
        return await self._results.query(project_id, res_query)


class ExpectationsQueryProvider(QueryProvider):
    """Query provider serving what='expectations' via Expectations.states."""

    what: QueryWhat = "expectations"

    def __init__(self, expectations: ExpectationsProtocol) -> None:
        self._expectations = expectations

    async def query(self, project_id: str, q: MonitoringQuery) -> Page[ExpectationState]:
        all_states = await self._expectations.states(project_id)

        filtered = all_states
        if q.id is not None:
            filtered = [s for s in filtered if s.key.ref == q.id]
        if q.model is not None:
            filtered = [s for s in filtered if s.key.model == q.model]
        if q.subject is not None:
            filtered = [s for s in filtered if s.key.ref == q.subject]
        if q.status is not None:
            filtered = [s for s in filtered if s.status == q.status]
        if q.since is not None:
            filtered = [
                s
                for s in filtered
                if (s.since is not None and s.since >= q.since)
                or (s.last_seen is not None and s.last_seen >= q.since)
            ]

        total = len(filtered)
        items = filtered[q.offset : q.offset + q.limit]
        truncated = (q.offset + len(items)) < total
        return Page(rows=items, total=total, truncated=truncated)
