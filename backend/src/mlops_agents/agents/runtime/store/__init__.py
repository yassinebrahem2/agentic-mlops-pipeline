"""The session store: one entry point composing one module per table group."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from sqlalchemy import and_, insert, or_, select, update

from mlops_agents.agents.runtime.shielded import shielded
from mlops_agents.agents.runtime.store.messages import MessagesStore
from mlops_agents.agents.runtime.store.turns import TurnsStore
from mlops_agents.agents.runtime.store.usage import UsageStore
from mlops_agents.clock import now_iso
from mlops_agents.ids import new_id
from mlops_agents.storage.tables import messages, sessions, turns

__all__ = ["SessionStore"]


class SessionStore(MessagesStore, TurnsStore, UsageStore):
    """The sessions table, and recovery across turns and messages."""

    def db_path(self, project_id: str) -> Path:
        """Return the filesystem Path to the project database."""
        return self._databases.paths.project_db(project_id)

    @shielded
    async def session_id(self, project_id: str, agent: str) -> str:
        """Get or create a stable session for the given agent."""
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            row = (
                await conn.execute(select(sessions.c.id).where(sessions.c.agent == agent))
            ).first()
            if row is not None:
                return str(row[0])

            sid = new_id("ses")
            await conn.execute(
                insert(sessions).values(
                    id=sid,
                    agent=agent,
                    summary=None,
                    compacted_through=None,
                    created_at=now_iso(),
                )
            )
            return sid

    @shielded
    async def recover(self, project_id: str) -> None:
        """Mark running turns and their incomplete messages (streaming/running/stopped) as interrupted."""
        engine = await self._databases.project(project_id)
        now = now_iso()
        async with engine.begin() as conn:
            running_turn_ids = (
                (await conn.execute(select(turns.c.id).where(turns.c.status == "running")))
                .scalars()
                .all()
            )

            if running_turn_ids:
                await conn.execute(
                    update(turns)
                    .where(turns.c.id.in_(running_turn_ids))
                    .values(status="interrupted", ended_at=now)
                )
                await conn.execute(
                    update(messages)
                    .where(
                        or_(
                            and_(
                                messages.c.turn_id.in_(running_turn_ids),
                                messages.c.status.in_(["streaming", "running", "stopped"]),
                            ),
                            messages.c.status == "streaming",
                        )
                    )
                    .values(status="interrupted")
                )
            else:
                await conn.execute(
                    update(messages)
                    .where(messages.c.status == "streaming")
                    .values(status="interrupted")
                )

    @shielded
    async def set_summary(
        self, project_id: str, session_id: str, summary: str, compacted_through: str
    ) -> None:
        """Update summary and compacted_through on the session row."""
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                update(sessions)
                .where(sessions.c.id == session_id)
                .values(summary=summary, compacted_through=compacted_through)
            )

    async def summary(self, project_id: str, session_id: str) -> tuple[str | None, str | None]:
        """Return (summary, compacted_through) for session_id."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    select(sessions.c.summary, sessions.c.compacted_through).where(
                        sessions.c.id == session_id
                    )
                )
            ).first()

        if row is None:
            return (None, None)
        return (cast(str | None, row[0]), cast(str | None, row[1]))
