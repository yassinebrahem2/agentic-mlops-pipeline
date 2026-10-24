from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from pydantic import TypeAdapter
from sqlalchemy import and_, insert, or_, select, update

from mlops_agents.agents.runtime.contract import (
    AuthorKind,
    MessageStatus,
    MessageView,
    Part,
)
from mlops_agents.agents.runtime.shielded import shielded
from mlops_agents.clock import now_iso
from mlops_agents.ids import new_id
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import llm_calls, messages, sessions, turns

_PARTS_ADAPTER: TypeAdapter[list[Part]] = TypeAdapter(list[Part])


class SessionStore:
    def __init__(self, databases: Databases) -> None:
        self._databases = databases

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
    async def add_message(
        self,
        project_id: str,
        session_id: str,
        *,
        author_kind: AuthorKind,
        author_name: str,
        parts: Sequence[Part],
        status: MessageStatus,
        turn_id: str | None = None,
        from_agent: str | None = None,
        exchange_id: str | None = None,
        hop: int | None = None,
    ) -> MessageView:
        """Insert a new message row and return its view."""
        engine = await self._databases.project(project_id)
        msg_id = new_id("msg")
        created_at = now_iso()
        parts_list = list(parts)
        parts_json = _PARTS_ADAPTER.dump_json(parts_list).decode("utf-8")

        async with engine.begin() as conn:
            await conn.execute(
                insert(messages).values(
                    id=msg_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    author_kind=author_kind,
                    author_name=author_name,
                    from_agent=from_agent,
                    parts=parts_json,
                    status=status,
                    created_at=created_at,
                    exchange_id=exchange_id,
                    hop=hop,
                )
            )

        return MessageView(
            id=msg_id,
            turn_id=turn_id,
            author_kind=author_kind,
            author_name=author_name,
            from_agent=from_agent,
            parts=parts_list,
            status=status,
            created_at=created_at,
        )

    @shielded
    async def update_message(
        self,
        project_id: str,
        message_id: str,
        *,
        parts: Sequence[Part] | None = None,
        status: MessageStatus | None = None,
    ) -> MessageView:
        """Update parts and/or status of an existing message."""
        engine = await self._databases.project(project_id)
        values: dict[str, Any] = {}
        if parts is not None:
            values["parts"] = _PARTS_ADAPTER.dump_json(list(parts)).decode("utf-8")
        if status is not None:
            values["status"] = status

        async with engine.begin() as conn:
            if values:
                await conn.execute(
                    update(messages).where(messages.c.id == message_id).values(**values)
                )
            row = (
                (await conn.execute(select(messages).where(messages.c.id == message_id)))
                .mappings()
                .first()
            )

        if row is None:
            raise ValueError(f"Message {message_id} not found in project {project_id}")

        deserialized_parts = _PARTS_ADAPTER.validate_json(cast(str, row["parts"]))
        return MessageView(
            id=str(row["id"]),
            turn_id=cast(str | None, row["turn_id"]),
            author_kind=cast(AuthorKind, row["author_kind"]),
            author_name=cast(str, row["author_name"]),
            from_agent=cast(str | None, row["from_agent"]),
            parts=deserialized_parts,
            status=cast(MessageStatus, row["status"]),
            created_at=cast(str, row["created_at"]),
        )

    async def messages(
        self, project_id: str, session_id: str, *, after: str | None = None
    ) -> list[MessageView]:
        """Return messages for session_id in ascending seq order, optionally after a message id."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            query = (
                select(messages)
                .where(messages.c.session_id == session_id)
                .order_by(messages.c.seq.asc())
            )
            if after is not None:
                after_seq = await conn.scalar(
                    select(messages.c.seq).where(
                        messages.c.id == after, messages.c.session_id == session_id
                    )
                )
                if after_seq is None:
                    return []
                query = query.where(messages.c.seq > after_seq)

            rows = (await conn.execute(query)).mappings().all()

        return [
            MessageView(
                id=str(row["id"]),
                turn_id=cast(str | None, row["turn_id"]),
                author_kind=cast(AuthorKind, row["author_kind"]),
                author_name=cast(str, row["author_name"]),
                from_agent=cast(str | None, row["from_agent"]),
                parts=_PARTS_ADAPTER.validate_json(cast(str, row["parts"])),
                status=cast(MessageStatus, row["status"]),
                created_at=cast(str, row["created_at"]),
            )
            for row in rows
        ]

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

    @shielded
    async def mark_interrupted(self, project_id: str, turn_id: str) -> None:
        """Mark streaming or stopped messages for a turn as interrupted."""
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                update(messages)
                .where(
                    and_(
                        messages.c.turn_id == turn_id,
                        messages.c.status.in_(["streaming", "stopped"]),
                    )
                )
                .values(status="interrupted")
            )

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

    @shielded
    async def add_llm_call(
        self,
        project_id: str,
        turn_id: str,
        *,
        agent: str,
        model_address: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        latency_ms: int,
    ) -> None:
        """Insert a record into the llm_calls table."""
        engine = await self._databases.project(project_id)
        call_id = new_id("llm")
        created_at = now_iso()
        async with engine.begin() as conn:
            await conn.execute(
                insert(llm_calls).values(
                    id=call_id,
                    turn_id=turn_id,
                    agent=agent,
                    model_address=model_address,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_ms=latency_ms,
                    created_at=created_at,
                )
            )

    async def last_llm_call_prompt_tokens(
        self, project_id: str, session_id: str
    ) -> tuple[int, str, str | None, str | None] | None:
        """Return (prompt_tokens, created_at, turn_id, trigger_message_id) of last LLM call, or None."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        llm_calls.c.prompt_tokens,
                        llm_calls.c.created_at,
                        llm_calls.c.turn_id,
                        turns.c.trigger_message_id,
                    )
                    .select_from(llm_calls.join(turns, llm_calls.c.turn_id == turns.c.id))
                    .where(turns.c.session_id == session_id, llm_calls.c.prompt_tokens.is_not(None))
                    .order_by(llm_calls.c.created_at.desc(), llm_calls.c.id.desc())
                    .limit(1)
                )
            ).first()

        if row is None or row[0] is None:
            return None
        return (
            int(row[0]),
            str(row[1]),
            str(row[2]) if row[2] is not None else None,
            str(row[3]) if row[3] is not None else None,
        )
