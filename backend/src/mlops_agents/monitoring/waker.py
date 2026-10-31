"""AgentWaker implementation: deduplicates, rate-limits and posts system wake messages."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Final

from sqlalchemy import delete, func, insert, select, update

from mlops_agents.agents.runtime.contract import AgentSessions
from mlops_agents.clock import to_iso
from mlops_agents.events.contract import EventLog
from mlops_agents.monitoring.incidents.contract import AgentWaker, WakeLabel
from mlops_agents.monitoring.settings import MAX_WAKES_PER_HOUR
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import agent_wakes

_logger = logging.getLogger(__name__)

WAKER_AUTHOR: Final[str] = "system"


class Waker(AgentWaker):
    """Posts system wake-up messages to agent sessions, deduplicating by key and enforcing rate limits."""

    def __init__(self, databases: Databases, events: EventLog, clock: Clock) -> None:
        self._databases = databases
        self._events = events
        self._clock = clock
        self._sessions: AgentSessions | None = None

    def bind(self, sessions: AgentSessions) -> None:
        """Binds the session manager required to post wake messages."""
        self._sessions = sessions

    @property
    def is_bound(self) -> bool:
        """Returns True if an AgentSessions instance is bound."""
        return self._sessions is not None

    @property
    def _sessions_bound(self) -> AgentSessions:
        if self._sessions is None:
            raise RuntimeError("AgentSessions not bound to Waker.")
        return self._sessions

    async def wake(
        self,
        project_id: str,
        agent: str,
        label: WakeLabel,
        subject_id: str,
        text: str,
        key: str,
    ) -> None:
        """Wakes an agent with a system message if not already woken and under hourly limit."""
        engine = await self._databases.project(project_id)
        now = self._clock.now()

        should_skip = False
        async with engine.begin() as conn:
            existing = (
                await conn.execute(
                    select(agent_wakes.c.agent).where(
                        agent_wakes.c.agent == agent,
                        agent_wakes.c.label == label,
                        agent_wakes.c.subject == subject_id,
                        agent_wakes.c.key == key,
                    )
                )
            ).first()
            if existing is not None:
                return

            cutoff = to_iso(now - timedelta(hours=1))
            count = (
                await conn.execute(
                    select(func.count())
                    .select_from(agent_wakes)
                    .where(agent_wakes.c.created >= cutoff)
                )
            ).scalar_one()

            if count >= MAX_WAKES_PER_HOUR:
                should_skip = True
            else:
                await conn.execute(
                    insert(agent_wakes).values(
                        agent=agent,
                        label=label,
                        subject=subject_id,
                        key=key,
                        text=text,
                        created=to_iso(now),
                        posted_at=None,
                    )
                )

        if should_skip:
            await self._events.record(
                project_id,
                source="system",
                type="incident.wake_skipped",
                subject_id=subject_id,
                payload={"agent": agent, "label": label, "key": key},
            )
            return

        try:
            await self._sessions_bound.post(
                project_id,
                agent,
                author_name=WAKER_AUTHOR,
                from_agent=WAKER_AUTHOR,
                text=text,
                exchange_id=None,
                hop=None,
                system=True,
            )
        except Exception:
            async with engine.begin() as conn:
                await conn.execute(
                    delete(agent_wakes).where(
                        agent_wakes.c.agent == agent,
                        agent_wakes.c.label == label,
                        agent_wakes.c.subject == subject_id,
                        agent_wakes.c.key == key,
                    )
                )
            raise

        posted_at = to_iso(self._clock.now())
        async with engine.begin() as conn:
            await conn.execute(
                update(agent_wakes)
                .where(
                    agent_wakes.c.agent == agent,
                    agent_wakes.c.label == label,
                    agent_wakes.c.subject == subject_id,
                    agent_wakes.c.key == key,
                )
                .values(posted_at=posted_at)
            )

    async def resume(self, project_id: str) -> None:
        """At startup, re-posts rows left with posted_at IS NULL once from their stored text."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(
                        agent_wakes.c.agent,
                        agent_wakes.c.label,
                        agent_wakes.c.subject,
                        agent_wakes.c.key,
                        agent_wakes.c.text,
                    )
                    .where(agent_wakes.c.posted_at.is_(None))
                    .order_by(agent_wakes.c.created.asc())
                )
            ).all()

        for row in rows:
            try:
                await self._sessions_bound.post(
                    project_id,
                    row.agent,
                    author_name=WAKER_AUTHOR,
                    from_agent=WAKER_AUTHOR,
                    text=row.text,
                    exchange_id=None,
                    hop=None,
                    system=True,
                )
                posted_at = to_iso(self._clock.now())
                async with engine.begin() as conn:
                    await conn.execute(
                        update(agent_wakes)
                        .where(
                            agent_wakes.c.agent == row.agent,
                            agent_wakes.c.label == row.label,
                            agent_wakes.c.subject == row.subject,
                            agent_wakes.c.key == row.key,
                        )
                        .values(posted_at=posted_at)
                    )
            except Exception:
                _logger.exception(
                    "Failed to post leftover wake for %s to %s during resume",
                    row.subject,
                    row.agent,
                )
