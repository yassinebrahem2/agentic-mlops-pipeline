from __future__ import annotations

from typing import Any

from sqlalchemy import insert, select, update

from mlops_agents.agents.runtime.shielded import shielded
from mlops_agents.agents.runtime.store.base import StoreBase
from mlops_agents.clock import now_iso
from mlops_agents.ids import new_id
from mlops_agents.storage.tables import turns


class TurnsStore(StoreBase):
    """The turns table."""

    @shielded
    async def add_turn(
        self, project_id: str, session_id: str, trigger_message_id: str, status: str
    ) -> str:
        """Insert a turn row and return its turn id."""
        engine = await self._databases.project(project_id)
        turn_id = new_id("trn")
        created_at = now_iso()
        started_at = created_at if status == "running" else None
        ended_at = created_at if status in ("done", "stopped", "interrupted", "failed") else None

        async with engine.begin() as conn:
            await conn.execute(
                insert(turns).values(
                    id=turn_id,
                    session_id=session_id,
                    trigger_message_id=trigger_message_id,
                    status=status,
                    error=None,
                    created_at=created_at,
                    started_at=started_at,
                    ended_at=ended_at,
                )
            )
        return turn_id

    @shielded
    async def set_turn(
        self, project_id: str, turn_id: str, status: str, *, error: str | None = None
    ) -> None:
        """Update turn status and error, setting started_at/ended_at appropriately."""
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            current = (
                (await conn.execute(select(turns).where(turns.c.id == turn_id))).mappings().first()
            )
            if current is None:
                return

            values: dict[str, Any] = {"status": status}
            if error is not None:
                values["error"] = error

            now = now_iso()
            if status == "running" and current["started_at"] is None:
                values["started_at"] = now
            if status in ("done", "stopped", "interrupted", "failed"):
                values["ended_at"] = now

            await conn.execute(update(turns).where(turns.c.id == turn_id).values(**values))

    async def next_queued_turn(self, project_id: str, session_id: str) -> tuple[str, str] | None:
        """Find the oldest queued turn for session_id. Returns (turn_id, trigger_message_id) or None."""
        engine = await self._databases.project(project_id)
        query = (
            select(turns.c.id, turns.c.trigger_message_id)
            .where(turns.c.session_id == session_id, turns.c.status == "queued")
            .order_by(turns.c.created_at.asc(), turns.c.id.asc())
            .limit(1)
        )
        async with engine.connect() as conn:
            row = (await conn.execute(query)).first()

        if row is None:
            return None
        return (str(row[0]), str(row[1]))

    async def has_waiting_turn(self, project_id: str, session_id: str) -> bool:
        """Whether a turn of session_id is paused, waiting for an approval."""
        engine = await self._databases.project(project_id)
        query = (
            select(turns.c.id)
            .where(turns.c.session_id == session_id, turns.c.status == "waiting")
            .limit(1)
        )
        async with engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return row is not None

    async def waiting_turn(self, project_id: str, session_id: str) -> tuple[str, str] | None:
        """Find the waiting turn for session_id. Returns (turn_id, trigger_message_id) or None."""
        engine = await self._databases.project(project_id)
        query = (
            select(turns.c.id, turns.c.trigger_message_id)
            .where(turns.c.session_id == session_id, turns.c.status == "waiting")
            .limit(1)
        )
        async with engine.connect() as conn:
            row = (await conn.execute(query)).first()
        if row is None:
            return None
        return (str(row[0]), str(row[1]))

    async def turn_status(self, project_id: str, turn_id: str) -> str | None:
        """Return the status of turn_id, or None if not found."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (await conn.execute(select(turns.c.status).where(turns.c.id == turn_id))).first()
        return None if row is None else str(row[0])

    async def turn_trigger_id(self, project_id: str, turn_id: str) -> str | None:
        """Return the trigger_message_id of turn_id, or None if not found."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                await conn.execute(select(turns.c.trigger_message_id).where(turns.c.id == turn_id))
            ).first()
        return None if row is None else str(row[0])
