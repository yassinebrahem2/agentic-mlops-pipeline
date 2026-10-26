"""Scheduled wake-ups and in-process timer (Task 4)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, insert, select, update

from mlops_agents.ids import new_id
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import projects, wakeups

if TYPE_CHECKING:
    from mlops_agents.agents.catalog import AgentCatalog
    from mlops_agents.agents.runtime.contract import AgentSessions
    from mlops_agents.events.contract import EventLog

logger = logging.getLogger(__name__)

MIN_DELAY_SECONDS = 60
MAX_DELAY_SECONDS = 7 * 86400  # 7 days = 604800 seconds
MAX_PENDING_PER_AGENT = 5
MAX_FIRED_PER_ROLLING_DAY = 24


class WakeupLimitReached(Exception):
    """Raised when scheduling a wake-up exceeds configured limits."""


class WakeupPermissionError(Exception):
    """Raised when an agent is not permitted to schedule wake-ups."""


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


class WakeupService:
    """Manages scheduled wake-ups and runs the single timer task that fires due wake-ups."""

    def __init__(
        self,
        databases: Databases,
        *,
        sessions: AgentSessions | None = None,
        events: EventLog | None = None,
        catalog: AgentCatalog | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._databases = databases
        self._sessions = sessions
        self._events = events
        self._catalog = catalog
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._wake_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._known_projects: set[str] = set()

    def bind(self, sessions: AgentSessions) -> None:
        """Bind the session manager (needed to post wake-up messages)."""
        self._sessions = sessions

    def bind_sessions(self, sessions: AgentSessions) -> None:
        """Alias for bind."""
        self.bind(sessions)

    def use_catalog(self, catalog: AgentCatalog) -> None:
        """Attach the agent catalog for permission checks."""
        self._catalog = catalog

    @property
    def catalog(self) -> AgentCatalog | None:
        return self._catalog

    def notify_sooner(self) -> None:
        """Signal the timer loop that a new wake-up may be due earlier than the current sleep."""
        self._wake_event.set()

    async def _get_all_project_ids(self) -> list[str]:
        pids = set(self._known_projects)
        try:
            app_engine = await self._databases.app()
            async with app_engine.connect() as conn:
                rows = (await conn.execute(select(projects.c.id))).fetchall()
                pids.update(str(r[0]) for r in rows)
        except Exception:
            logger.debug("Failed to list projects from app database", exc_info=True)
        return list(pids)

    async def schedule(
        self,
        project_id: str,
        *,
        session_id: str,
        agent: str,
        delay_seconds: int,
        note: str,
    ) -> str:
        """Schedule a wake-up for an agent after delay_seconds."""
        if not note.strip():
            raise ValueError("Wake-up note cannot be empty.")
        if delay_seconds < MIN_DELAY_SECONDS:
            raise WakeupLimitReached(f"delay_seconds must be at least {MIN_DELAY_SECONDS} seconds.")
        if delay_seconds > MAX_DELAY_SECONDS:
            raise WakeupLimitReached(
                f"delay_seconds must be at most {MAX_DELAY_SECONDS} seconds (7 days)."
            )

        if self._catalog is not None:
            caller_def = self._catalog.get(agent)
            if not caller_def.schedules:
                raise WakeupPermissionError(f"Agent '{agent}' is not allowed to schedule wake-ups.")

        engine = await self._databases.project(project_id)
        now = self._clock()

        async with engine.connect() as conn:
            pending_count = await conn.scalar(
                select(func.count())
                .select_from(wakeups)
                .where(wakeups.c.agent == agent, wakeups.c.status == "pending")
            )
            if (pending_count or 0) >= MAX_PENDING_PER_AGENT:
                raise WakeupLimitReached(
                    f"Agent '{agent}' has reached the limit of {MAX_PENDING_PER_AGENT} pending wake-ups."
                )

            cutoff_iso = _to_iso(now - timedelta(hours=24))
            fired_count = await conn.scalar(
                select(func.count())
                .select_from(wakeups)
                .where(
                    wakeups.c.agent == agent,
                    wakeups.c.status == "fired",
                    wakeups.c.fired_at >= cutoff_iso,
                )
            )
            if (fired_count or 0) >= MAX_FIRED_PER_ROLLING_DAY:
                raise WakeupLimitReached(
                    f"Agent '{agent}' has reached the limit of {MAX_FIRED_PER_ROLLING_DAY} "
                    "fired wake-ups in a rolling 24-hour period."
                )

        wak_id = new_id("wak")
        due_at_iso = _to_iso(now + timedelta(seconds=delay_seconds))
        created_at_iso = _to_iso(now)

        async with engine.begin() as conn:
            await conn.execute(
                insert(wakeups).values(
                    id=wak_id,
                    session_id=session_id,
                    agent=agent,
                    note=note,
                    due_at=due_at_iso,
                    status="pending",
                    created_at=created_at_iso,
                    fired_at=None,
                )
            )

        self._known_projects.add(project_id)

        if self._events is not None:
            await self._events.record(
                project_id,
                source=agent,
                type="wakeup.scheduled",
                subject_id=wak_id,
                payload={"wakeup_id": wak_id, "due_at": due_at_iso},
            )

        self.notify_sooner()
        return wak_id

    async def fire_due(self) -> int:
        """Fire any pending wake-ups whose due_at is on or before the current clock time."""
        if self._sessions is None:
            return 0

        now = self._clock()
        now_iso_str = _to_iso(now)
        fired_count = 0

        project_ids = await self._get_all_project_ids()
        for project_id in project_ids:
            try:
                engine = await self._databases.project(project_id)
            except Exception:
                logger.debug("Failed to connect to project %s database", project_id, exc_info=True)
                continue

            async with engine.connect() as conn:
                rows = (
                    (
                        await conn.execute(
                            select(
                                wakeups.c.id,
                                wakeups.c.session_id,
                                wakeups.c.agent,
                                wakeups.c.note,
                                wakeups.c.due_at,
                            )
                            .where(
                                wakeups.c.status == "pending",
                                wakeups.c.due_at <= now_iso_str,
                            )
                            .order_by(wakeups.c.due_at.asc())
                        )
                    )
                    .mappings()
                    .all()
                )

            for row in rows:
                wak_id = str(row["id"])
                agent = str(row["agent"])
                note = str(row["note"])

                async with engine.begin() as conn:
                    res = await conn.execute(
                        update(wakeups)
                        .where(wakeups.c.id == wak_id, wakeups.c.status == "pending")
                        .values(status="fired", fired_at=now_iso_str)
                    )
                    if res.rowcount == 0:
                        continue

                await self._sessions.post(
                    project_id,
                    agent,
                    author_name="Scheduled wake-up",
                    from_agent=agent,
                    text=f"Scheduled wake-up: {note}",
                    exchange_id=None,
                    hop=None,
                )
                fired_count += 1

        return fired_count

    async def next_due_time(self) -> datetime | None:
        """Find the earliest pending wake-up across all projects."""
        earliest: datetime | None = None
        project_ids = await self._get_all_project_ids()
        for project_id in project_ids:
            try:
                engine = await self._databases.project(project_id)
            except Exception:
                logger.debug("Failed to connect to project %s database", project_id, exc_info=True)
                continue

            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        select(wakeups.c.due_at)
                        .where(wakeups.c.status == "pending")
                        .order_by(wakeups.c.due_at.asc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if row is not None:
                    dt = _parse_iso(str(row))
                    if earliest is None or dt < earliest:
                        earliest = dt
        return earliest

    async def _run_loop(self) -> None:
        """Timer loop: fires due wakeups and sleeps until the next pending one."""
        while not self._stopped:
            try:
                await self.fire_due()
                next_due = await self.next_due_time()
                if next_due is None:
                    await self._wake_event.wait()
                    self._wake_event.clear()
                else:
                    now = self._clock()
                    delay = (next_due - now).total_seconds()
                    if delay > 0:
                        try:
                            await asyncio.wait_for(self._wake_event.wait(), timeout=delay)
                        except TimeoutError:
                            pass
                        finally:
                            self._wake_event.clear()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in wakeup timer loop")
                try:
                    await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    break

    async def start(self) -> asyncio.Task[None]:
        """Start the timer. Wake-ups due during downtime fire at once."""
        self._stopped = False
        await self.fire_due()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop())
        return self._task

    async def close(self) -> None:
        """Stop and cancel the timer task."""
        self._stopped = True
        self.notify_sooner()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None


WakeupManager = WakeupService
