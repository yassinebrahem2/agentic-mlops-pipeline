"""The kickoff: one "Handoff" message to each audit agent when a handoff is approved.

It is posted the way stage 2's wake-ups are: the recipient is its own `from_agent` and there is
no exchange, so no hop or token limit applies, replies stay in the agent's own session and
nothing wakes Onboarding. Each post is recorded per (handoff version, agent) in the `kickoff`
table, so a restart neither repeats nor loses one.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from mlops_agents.agents.runtime.contract import AgentSessions
from mlops_agents.clock import now_iso
from mlops_agents.onboarding.briefs import (
    AUDIT_BRIEFS,
    BRIEF_SECTIONS,
    CUT_LINE,
    KICKOFF_AGENTS,
    KICKOFF_MESSAGE_CAP,
)
from mlops_agents.onboarding.handoff_preview import sections
from mlops_agents.proposals.contract import Handoffs
from mlops_agents.proposals.payloads import HandoffPayload
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.migrate import MigrationError
from mlops_agents.storage.tables import kickoff, messages, projects

logger = logging.getLogger(__name__)

HANDOFF_AUTHOR: Final = "Handoff"

# What opening a project's database raises when it cannot be opened: a corrupt or unreadable
# file, or a schema from a newer app.
_OPEN_ERRORS: Final = (MigrationError, sqlite3.Error, SQLAlchemyError, OSError)


def _marker(proposal_id: str) -> str:
    return f"[handoff {proposal_id}]"


def kickoff_text(agent: str, proposal_id: str, payload: HandoffPayload) -> str:
    """The marker, then the handoff summary with the agent's own sections first, then its brief.

    Past the cap, the summary keeps the agent's own sections and as many of the rest, in order,
    as fit, and ends with the cut line.
    """
    parts = sections(payload)
    own = list(BRIEF_SECTIONS[agent])
    order = own + [key for key in parts if key not in own]

    def compose(keys: list[str], *, cut: bool) -> str:
        blocks = [_marker(proposal_id), *(parts[key] for key in keys)]
        if cut:
            blocks.append(CUT_LINE)
        blocks.append(AUDIT_BRIEFS[agent])
        return "\n\n".join(blocks)

    whole = compose(order, cut=False)
    if len(whole) <= KICKOFF_MESSAGE_CAP:
        return whole
    kept = list(own)
    for key in order[len(own) :]:
        if len(compose([*kept, key], cut=True)) > KICKOFF_MESSAGE_CAP:
            break
        kept.append(key)
    logger.info(
        "The kickoff message to %s for handoff %s was cut at %d characters: %d of %d sections kept",
        agent,
        proposal_id,
        KICKOFF_MESSAGE_CAP,
        len(kept),
        len(order),
    )
    return compose(kept, cut=True)


class Kickoff:
    """Posts each approved handoff's kickoff exactly once, and finishes any lost to a crash."""

    def __init__(self, databases: Databases, sessions: AgentSessions, handoffs: Handoffs) -> None:
        self._databases = databases
        self._sessions = sessions
        self._handoffs = handoffs
        # One lock per (project, handoff version): `start` and `resume` never run one kickoff
        # twice at once.
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}

    def _lock(self, project_id: str, version: int) -> asyncio.Lock:
        return self._locks.setdefault((project_id, version), asyncio.Lock())

    async def start(
        self, project_id: str, version: int, proposal_id: str, payload: HandoffPayload
    ) -> list[str]:
        """Post the kickoff of this handoff version; returns the agents posted by this call."""
        async with self._lock(project_id, version):
            return await self._run(project_id, version, proposal_id, payload)

    async def resume(self) -> None:
        """At startup: finish the kickoff of every project's current handoff."""
        for project_id in await self._project_ids():
            try:
                current = await self._handoffs.current(project_id)
            except _OPEN_ERRORS:
                logger.warning(
                    "Skipped the kickoff of project %s: its database cannot be opened",
                    project_id,
                    exc_info=True,
                )
                continue
            if current is None:
                continue
            async with self._lock(project_id, current.version):
                await self._run(project_id, current.version, current.proposal_id, current.payload)

    async def _project_ids(self) -> list[str]:
        engine = await self._databases.app()
        async with engine.connect() as conn:
            return [str(pid) for pid in (await conn.execute(select(projects.c.id))).scalars()]

    async def _run(
        self, project_id: str, version: int, proposal_id: str, payload: HandoffPayload
    ) -> list[str]:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            # The intent first, so a crash before any post still leaves the rows to finish.
            await conn.execute(
                sqlite_insert(kickoff)
                .values(
                    [
                        {"handoff_version": version, "agent": agent, "status": "pending"}
                        for agent in KICKOFF_AGENTS
                    ]
                )
                .on_conflict_do_nothing(index_elements=[kickoff.c.handoff_version, kickoff.c.agent])
            )
            pending = set(
                (
                    await conn.execute(
                        select(kickoff.c.agent).where(
                            kickoff.c.handoff_version == version, kickoff.c.status == "pending"
                        )
                    )
                ).scalars()
            )
        agents = [agent for agent in KICKOFF_AGENTS if agent in pending]
        # All at once, so the audits start together.
        posted = await asyncio.gather(
            *(self._kick(engine, project_id, version, proposal_id, payload, a) for a in agents)
        )
        return [agent for agent, now in zip(agents, posted, strict=True) if now]

    async def _kick(
        self,
        engine: AsyncEngine,
        project_id: str,
        version: int,
        proposal_id: str,
        payload: HandoffPayload,
        agent: str,
    ) -> bool:
        """Post to one agent unless an earlier run already did; True when posted now."""
        earlier = await self._earlier_post(engine, agent, proposal_id)
        if earlier is not None:  # posted before a crash, never marked
            await self._mark(engine, version, agent, earlier)
            return False
        try:
            message = await self._sessions.post(
                project_id,
                agent,
                author_name=HANDOFF_AUTHOR,
                from_agent=agent,
                text=kickoff_text(agent, proposal_id, payload),
                exchange_id=None,
                hop=None,
            )
        except Exception:
            # The row stays pending: the next resume retries it. The other agents still go out.
            logger.exception(
                "The kickoff of handoff %s could not be posted to %s in project %s",
                proposal_id,
                agent,
                project_id,
            )
            return False
        await self._mark(engine, version, agent, message.id)
        return True

    async def _earlier_post(self, engine: AsyncEngine, agent: str, proposal_id: str) -> str | None:
        async with engine.connect() as conn:
            found = await conn.scalar(
                select(messages.c.id)
                .where(
                    messages.c.from_agent == agent,
                    messages.c.author_name == HANDOFF_AUTHOR,
                    messages.c.parts.contains(_marker(proposal_id), autoescape=True),
                )
                .order_by(messages.c.seq)
                .limit(1)
            )
        return None if found is None else str(found)

    async def _mark(self, engine: AsyncEngine, version: int, agent: str, message_id: str) -> None:
        async with engine.begin() as conn:
            await conn.execute(
                update(kickoff)
                .where(
                    kickoff.c.handoff_version == version,
                    kickoff.c.agent == agent,
                    kickoff.c.status == "pending",
                )
                .values(status="posted", message_id=message_id, posted_at=now_iso())
            )
