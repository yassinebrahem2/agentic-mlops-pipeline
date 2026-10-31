"""RetryJob for notification retries on the app scheduler."""

from __future__ import annotations

from datetime import UTC, datetime

from mlops_agents.monitoring.jobs import JobDue, JobKind
from mlops_agents.monitoring.scheduler.contract import ScheduledJob
from mlops_agents.monitoring.scheduler.times import interval_dues
from mlops_agents.monitoring.settings import get_monitoring_settings
from mlops_agents.notifications.notifier import SqliteNotifier
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases


class RetryJob(ScheduledJob):
    """Scheduled job to retry pending notifications."""

    kind: JobKind = JobKind.RETRY_NOTIFICATIONS
    interval_s: int = 60

    def __init__(
        self,
        databases: Databases,
        notifier: SqliteNotifier,
        clock: Clock,
    ) -> None:
        self._databases = databases
        self._notifier = notifier
        self._clock = clock

    async def due(self, project_id: str, after: datetime, now: datetime) -> list[JobDue]:
        """Gives one due per collection interval."""
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        if after.tzinfo is None:
            after = after.replace(tzinfo=UTC)

        settings = await get_monitoring_settings(self._databases, project_id)
        due_times = interval_dues(settings.collection_interval_s, after, now)
        return [JobDue(kind=self.kind, due=dt, subject="") for dt in due_times]

    async def run(self, project_id: str, job: JobDue) -> None:
        """Attempts every retrying row whose next attempt is at or before now."""
        now = self._clock.now()
        await self._notifier.retry_pending(project_id, now)
