"""Scheduler loop and central AppScheduler service (I24)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from mlops_agents.credentials.scrub import error_text
from mlops_agents.monitoring.jobs import JOB_ORDER, JobKind
from mlops_agents.monitoring.scheduler.constants import SCHEDULER_TICK_S
from mlops_agents.monitoring.scheduler.contract import ScheduledJob, SyncObserver
from mlops_agents.monitoring.scheduler.store import SchedulerStore
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases

logger = logging.getLogger(__name__)


class AppScheduler:
    """The central scheduler loop for all projects."""

    def __init__(
        self,
        databases: Databases,
        projects: Callable[[], Awaitable[list[str]]],
        clock: Clock,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tick_s: float = SCHEDULER_TICK_S,
    ) -> None:
        self._databases = databases
        self._projects = projects
        self._clock = clock
        self._sleep = sleep
        self._tick_s = tick_s
        self._store = SchedulerStore(databases)
        self._jobs: dict[JobKind, ScheduledJob] = {}
        self._sync_observers: list[SyncObserver] = []
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    @property
    def sync_observers(self) -> tuple[SyncObserver, ...]:
        """Observers kept in registration order for AFTER_SYNC."""
        return tuple(self._sync_observers)

    @property
    def task(self) -> asyncio.Task[None] | None:
        """The background loop task, if created."""
        return self._task

    def register(self, job: ScheduledJob) -> None:
        """Register a scheduled job."""
        self._jobs[job.kind] = job

    def add_sync_observer(self, observer: SyncObserver) -> None:
        """Keep observers in registration order for AFTER_SYNC."""
        self._sync_observers.append(observer)

    async def run_due(self, project_id: str, now: datetime) -> None:
        """Evaluate dues and execute unfinished jobs for project_id.

        For each registered job in JOB_ORDER:
          after is latest due stored, or (now - 1 interval) if none yet.
          insert dues with INSERT OR IGNORE.
        Then run every unfinished row in JOB_ORDER, then by due time.
        A row is finished only after run returns. If run raises, the scrubbed error
        is recorded on the row and it is not retried; subsequent jobs still run.
        """
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        now_utc = now.astimezone(UTC)

        # 1. Evaluate dues for registered jobs in JOB_ORDER
        for kind in JOB_ORDER:
            job = self._jobs.get(kind)
            if job is None:
                continue

            latest_due = await self._store.latest_due(project_id, kind)
            if latest_due is not None:
                after = latest_due
            else:
                interval_s = 60.0
                cand_interval: object = getattr(job, "interval_s", None)
                if isinstance(cand_interval, (int, float)):
                    interval_s = float(cand_interval)
                else:
                    cand_interval = getattr(job, "interval", None)
                    if isinstance(cand_interval, timedelta):
                        interval_s = cand_interval.total_seconds()
                    elif isinstance(cand_interval, (int, float)):
                        interval_s = float(cand_interval)
                after = now_utc - timedelta(seconds=interval_s)

            try:
                dues = await job.due(project_id, after, now_utc)
                await self._store.insert_dues(project_id, dues)
            except Exception:
                logger.exception("Failed to query dues for job %s on %s", kind, project_id)

        # 2. Run all unfinished rows in JOB_ORDER, then by due time
        unfinished = await self._store.unfinished_jobs(project_id)
        for row in unfinished:
            job = self._jobs.get(row.kind)
            if job is None:
                logger.warning("Unfinished job %s has no registered handler", row.kind)
                continue

            start_time = self._clock.now()
            await self._store.mark_started(project_id, row, start_time)
            try:
                await job.run(project_id, row)
                finish_time = self._clock.now()
                await self._store.mark_finished(project_id, row, finish_time)
            except Exception as exc:
                scrubbed_err = error_text(exc)
                await self._store.mark_error(project_id, row, scrubbed_err)
                logger.exception("Job %s (%s) raised for %s", row.kind, row.subject, project_id)

    async def run_pass(self) -> None:
        """Executes a single pass over all projects."""
        now = self._clock.now()
        try:
            project_ids = await self._projects()
        except Exception:
            logger.warning("Scheduler loop could not list projects", exc_info=True)
            return

        for project_id in project_ids:
            try:
                await self.run_due(project_id, now)
            except Exception:
                logger.exception("Scheduler run_due failed for project %s", project_id)

    async def _run_loop(self) -> None:
        """Continuous background loop running every tick_s seconds."""
        while not self._stopped:
            try:
                await self.run_pass()
                if self._stopped:
                    break
                await self._sleep(self._tick_s)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in scheduler loop pass")
                try:
                    await self._sleep(1.0)
                except asyncio.CancelledError:
                    break

    async def start(self) -> None:
        """Start the background scheduler task."""
        self._stopped = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stops and cancels the background scheduler task."""
        self._stopped = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
