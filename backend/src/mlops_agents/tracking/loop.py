"""Scheduled sync loop for tracking sources (Task 4)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from mlops_agents.tracking.contract import TrackingSourceView

logger = logging.getLogger(__name__)

TICK_S: Final[float] = 15.0


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _is_due(
    now: datetime,
    last_sync: datetime | None,
    last_attempt: datetime | None,
    interval_s: int,
) -> bool:
    if last_sync is None and last_attempt is None:
        return True
    if last_sync is None:
        ref = _to_utc(last_attempt)  # type: ignore[arg-type]
    elif last_attempt is None:
        ref = _to_utc(last_sync)
    else:
        ref = max(_to_utc(last_sync), _to_utc(last_attempt))
    return (_to_utc(now) - ref).total_seconds() >= interval_s


class TrackingServiceLike(Protocol):
    async def sources(self, project_id: str) -> list[TrackingSourceView]: ...
    async def sync_interval(self, project_id: str) -> int: ...
    async def run_sync(self, project_id: str, source_id: str | None = None) -> None: ...


class TrackingLoop:
    """Periodically syncs tracking sources for all active projects."""

    def __init__(
        self,
        tracking: TrackingServiceLike | Any,
        projects: Callable[[], Awaitable[Iterable[str]]],
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tick_s: float = TICK_S,
    ) -> None:
        self._tracking = tracking
        self._projects = projects
        self._clock = clock
        self._sleep = sleep
        self._tick_s = tick_s
        self._last_attempt: dict[tuple[str, str], datetime] = {}
        self._stopped = False
        self._task: asyncio.Task[None] | None = None

    async def run_pass(self) -> None:
        """Executes a single pass over all projects, syncing due sources."""
        project_ids = await self._projects()
        for project_id in project_ids:
            try:
                sources = await self._tracking.sources(project_id)
                if not sources:
                    continue
                interval = await self._tracking.sync_interval(project_id)
                for source in sources:
                    last_attempt = self._last_attempt.get((project_id, source.id))
                    now = self._clock()
                    if _is_due(now, source.last_sync, last_attempt, interval):
                        self._last_attempt[(project_id, source.id)] = now
                        try:
                            await self._tracking.run_sync(project_id, source.id)
                        except Exception:
                            logger.exception(
                                "Tracking sync failed for project %s source %s",
                                project_id,
                                source.id,
                            )
            except Exception:
                logger.warning(
                    "Skipping tracking sync for project %s: database or project unavailable",
                    project_id,
                    exc_info=True,
                )

    async def _run_loop(self) -> None:
        """Continuous loop running every tick_s seconds."""
        while not self._stopped:
            try:
                await self._sleep(self._tick_s)
                if self._stopped:
                    break
                await self.run_pass()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in tracking loop pass")
                try:
                    await self._sleep(1.0)
                except asyncio.CancelledError:
                    break

    async def start(self) -> asyncio.Task[None]:
        """Start the loop task. Initial pass runs immediately at startup."""
        self._stopped = False
        await self.run_pass()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop())
        return self._task

    async def close(self) -> None:
        """Stops and cancels the loop task."""
        self._stopped = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
