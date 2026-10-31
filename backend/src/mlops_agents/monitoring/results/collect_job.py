"""Scheduled job for collecting results on collection_interval_s grid (Task 4 - issue #460)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from mlops_agents.monitoring.jobs import JobDue, JobKind
from mlops_agents.monitoring.results.contract import CollectionReport
from mlops_agents.monitoring.scheduler.contract import ScheduledJob
from mlops_agents.monitoring.scheduler.times import interval_dues
from mlops_agents.monitoring.settings import get_monitoring_settings
from mlops_agents.storage.databases import Databases


class ResultsCollectProtocol(Protocol):
    async def collect(self, project_id: str, now: datetime) -> CollectionReport: ...


class CollectResultsJob(ScheduledJob):
    """Scheduled job that triggers results collection on the collection_interval_s grid."""

    kind: JobKind = JobKind.COLLECT_RESULTS
    interval_s: int = 60

    def __init__(self, databases: Databases, results: ResultsCollectProtocol) -> None:
        self._databases = databases
        self._results = results

    async def due(self, project_id: str, after: datetime, now: datetime) -> list[JobDue]:
        settings = await get_monitoring_settings(self._databases, project_id)
        points = interval_dues(settings.collection_interval_s, after, now)
        return [JobDue(kind=self.kind, due=p, subject="") for p in points]

    async def run(self, project_id: str, job: JobDue) -> None:
        await self._results.collect(project_id, job.due)
