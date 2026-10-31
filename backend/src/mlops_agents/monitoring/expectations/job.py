"""Scheduled job for checking expectations on collection_interval_s grid (Task 5 - issue #461)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from mlops_agents.monitoring.jobs import JobDue, JobKind
from mlops_agents.monitoring.scheduler.contract import ScheduledJob
from mlops_agents.monitoring.scheduler.times import interval_dues
from mlops_agents.monitoring.settings import get_monitoring_settings
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases


class ExpectationsEvaluateProtocol(Protocol):
    async def evaluate(self, project_id: str, now: datetime) -> list[str]: ...


class CheckExpectationsJob(ScheduledJob):
    """Scheduled job that evaluates monitoring expectations on the collection_interval_s grid."""

    kind: JobKind = JobKind.CHECK_EXPECTATIONS
    interval_s: int = 60

    def __init__(
        self,
        databases: Databases,
        expectations: ExpectationsEvaluateProtocol,
        clock: Clock | None = None,
    ) -> None:
        self._databases = databases
        self._expectations = expectations
        self._clock = clock

    async def due(self, project_id: str, after: datetime, now: datetime) -> list[JobDue]:
        settings = await get_monitoring_settings(self._databases, project_id)
        points = interval_dues(settings.collection_interval_s, after, now)
        return [JobDue(kind=self.kind, due=p, subject="") for p in points]

    async def run(self, project_id: str, job: JobDue) -> None:
        await self._expectations.evaluate(project_id, job.due)
