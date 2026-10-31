"""Shared fixtures and test adapters for monitoring rules tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from mlops_agents.monitoring.results.contract import (
    CollectionReport,
    ResultQuery,
    ResultRecord,
    Results,
    ResultsObserver,
    StoredResult,
)
from mlops_agents.monitoring.results.store import ResultsStore
from mlops_agents.tracking.contract import Page


class ResultsAdapter(Results):
    """Adapts ResultsStore to conform to Results protocol in tests."""

    def __init__(self, store: ResultsStore) -> None:
        self._store = store

    def add_observer(self, observer: ResultsObserver) -> None:
        pass

    async def ingest(
        self,
        project_id: str,
        source_id: str,
        items: Sequence[tuple[str, ResultRecord]],
        seen_at: datetime,
    ) -> list[StoredResult]:
        return await self._store.insert_items(project_id, source_id, items, seen_at)

    async def collect(self, project_id: str, now: datetime) -> CollectionReport:
        raise NotImplementedError

    async def query(self, project_id: str, q: ResultQuery) -> Page[StoredResult]:
        return await self._store.query(project_id, q)

    async def latest_per_check(
        self, project_id: str, subject: str, model: str | None
    ) -> list[StoredResult]:
        raise NotImplementedError
