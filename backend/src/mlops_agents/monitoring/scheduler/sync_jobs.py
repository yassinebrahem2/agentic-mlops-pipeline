"""Sync jobs for the scheduler: sync_tracking, sync_prs, after_sync (I24)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from mlops_agents.clock import utc_now
from mlops_agents.monitoring.jobs import JobDue, JobKind
from mlops_agents.monitoring.scheduler.contract import SyncObserver
from mlops_agents.monitoring.scheduler.times import interval_dues
from mlops_agents.monitoring.settings import get_monitoring_settings
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases

logger = logging.getLogger(__name__)


class TrackingServiceProtocol(Protocol):
    async def run_sync(
        self, project_id: str, source_id: str | None = None, *, check_project: bool = True
    ) -> None: ...

    async def sync_interval(self, project_id: str) -> int: ...


class PrSyncObserverProtocol(Protocol):
    async def sync_now(self, project_id: str) -> None: ...


@runtime_checkable
class SyncObserversProvider(Protocol):
    @property
    def sync_observers(self) -> Sequence[SyncObserver]: ...


class SyncTrackingJob:
    """Scheduled job for syncing tracking sources on tracking.sync_interval grid."""

    kind: JobKind = JobKind.SYNC_TRACKING
    interval_s: int = 60

    def __init__(self, tracking: TrackingServiceProtocol) -> None:
        self._tracking = tracking

    async def due(self, project_id: str, after: datetime, now: datetime) -> list[JobDue]:
        interval_s = await self._tracking.sync_interval(project_id)
        points = interval_dues(interval_s, after, now)
        return [JobDue(kind=self.kind, due=p, subject="") for p in points]

    async def run(self, project_id: str, job: JobDue) -> None:
        await self._tracking.run_sync(project_id)


class SyncPrsJob:
    """Scheduled job for syncing PR statuses on collection_interval_s grid."""

    kind: JobKind = JobKind.SYNC_PRS
    interval_s: int = 60

    def __init__(self, databases: Databases, pr_sync: PrSyncObserverProtocol) -> None:
        self._databases = databases
        self._pr_sync = pr_sync

    async def due(self, project_id: str, after: datetime, now: datetime) -> list[JobDue]:
        settings = await get_monitoring_settings(self._databases, project_id)
        points = interval_dues(settings.collection_interval_s, after, now)
        return [JobDue(kind=self.kind, due=p, subject="") for p in points]

    async def run(self, project_id: str, job: JobDue) -> None:
        await self._pr_sync.sync_now(project_id)


class AfterSyncJob:
    """Scheduled job running sync observers in registration order on collection_interval_s grid."""

    kind: JobKind = JobKind.AFTER_SYNC
    interval_s: int = 60

    def __init__(
        self,
        databases: Databases,
        observers: SyncObserversProvider
        | Callable[[], Sequence[SyncObserver]]
        | Sequence[SyncObserver],
        clock: Clock | None = None,
    ) -> None:
        self._databases = databases
        self._observers = observers
        self._clock = clock

    def _get_observers(self) -> Sequence[SyncObserver]:
        if isinstance(self._observers, SyncObserversProvider):
            return self._observers.sync_observers
        if callable(self._observers):
            return self._observers()
        return self._observers

    async def due(self, project_id: str, after: datetime, now: datetime) -> list[JobDue]:
        settings = await get_monitoring_settings(self._databases, project_id)
        points = interval_dues(settings.collection_interval_s, after, now)
        return [JobDue(kind=self.kind, due=p, subject="") for p in points]

    async def run(self, project_id: str, job: JobDue) -> None:
        observers = self._get_observers()
        now = self._clock.now() if self._clock is not None else utc_now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        now_utc = now.astimezone(UTC)

        errors: list[Exception] = []
        for observer in observers:
            try:
                await observer.synced(project_id, now_utc)
            except Exception as exc:
                logger.exception("SyncObserver %r failed on project %s", observer, project_id)
                errors.append(exc)

        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise RuntimeError("; ".join(str(e) for e in errors))
