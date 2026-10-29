"""The session store: one entry point composing one module per table group."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

from sqlalchemy import and_, insert, or_, select, update

from mlops_agents.agents.approvals.decisions import settle_decisions
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.contract import MessageView, Part
from mlops_agents.agents.runtime.shielded import shielded
from mlops_agents.agents.runtime.store.inbound import InboundStore
from mlops_agents.agents.runtime.store.messages import MessagesStore
from mlops_agents.agents.runtime.store.turns import TurnsStore
from mlops_agents.agents.runtime.store.usage import UsageStore
from mlops_agents.clock import now_iso
from mlops_agents.events.contract import AuditLog, EventLog
from mlops_agents.ids import new_id
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import messages, sessions, turns

__all__ = ["InboundStore", "SessionStore"]


class SessionStore(MessagesStore, TurnsStore, UsageStore, InboundStore):
    """The sessions table, and recovery across turns and messages."""

    def __init__(
        self,
        databases: Databases,
        audit_log: AuditLog | None = None,
        event_log: EventLog | None = None,
    ) -> None:
        super().__init__(databases)
        self._audit_log = audit_log
        self._event_log = event_log

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

    async def has_session(self, project_id: str, session_id: str) -> bool:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                await conn.execute(select(sessions.c.id).where(sessions.c.id == session_id))
            ).first()
        return row is not None

    async def session_agent(self, project_id: str, session_id: str) -> str | None:
        """Return the agent name for session_id, or None if not found."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                await conn.execute(select(sessions.c.agent).where(sessions.c.id == session_id))
            ).first()
        return None if row is None else str(row[0])

    async def session_agent_map(self, project_id: str) -> tuple[dict[str, str], set[str]]:
        """Return (session_to_agent map, waiting_session_ids)."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (await conn.execute(select(sessions.c.id, sessions.c.agent))).all()
            session_to_agent = {str(r[0]): str(r[1]) for r in rows}
            waiting_rows = (
                await conn.execute(
                    select(turns.c.session_id).where(turns.c.status == "waiting").distinct()
                )
            ).all()
            waiting_ids = {str(r[0]) for r in waiting_rows}
        return session_to_agent, waiting_ids

    @shielded
    async def recover(
        self,
        project_id: str,
        *,
        approval_store: ApprovalStore,
        audit_log: AuditLog | None = None,
        event_log: EventLog | None = None,
    ) -> None:
        """Mark running turns and their incomplete messages (streaming/stopped) as interrupted,
        and expire pending approvals whose turn is not waiting (through the app's one approval
        store, so its observers are told).
        """
        engine = await self._databases.project(project_id)
        now = now_iso()
        running_turns: list[tuple[str, str]] = []
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(turns.c.id, turns.c.session_id).where(turns.c.status == "running")
                )
            ).all()
            running_turns = [(str(r[0]), str(r[1])) for r in rows]
            running_turn_ids = [t[0] for t in running_turns]

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
                                messages.c.status.in_(["streaming", "stopped"]),
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

        for turn_id, session_id in running_turns:
            await self.redeliver_inbound(project_id, session_id, turn_id)

        # Expire pending approvals whose turn is not waiting; nothing runs yet, so their cards
        # are written without the session lock and without a publish.
        await settle_decisions(
            project_id,
            await approval_store.expire_non_waiting(project_id),
            store=approval_store,
            databases=self._databases,
            audit_log=audit_log or self._audit_log,
            event_log=event_log or self._event_log,
            rewrite_part=lambda session_id, message_id, transform: self.rewrite_parts(
                project_id, session_id, message_id, transform
            ),
        )

    async def rewrite_parts(
        self,
        project_id: str,
        session_id: str,
        message_id: str,
        transform: Callable[[list[Part]], list[Part]],
    ) -> MessageView | None:
        """Store `transform` of a message's parts; None when the message is gone. The caller
        holds the session lock, or no turn of the session runs (recovery)."""
        message = await self.message(project_id, session_id, message_id)
        if message is None:
            return None
        return await self.update_message(
            project_id, message_id, parts=transform(list(message.parts))
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
