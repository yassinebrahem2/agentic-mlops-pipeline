from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from pydantic import TypeAdapter
from sqlalchemy import RowMapping, Select, and_, insert, select, update

from mlops_agents.agents.runtime.contract import (
    AuthorKind,
    ErrorKind,
    MessageStatus,
    MessageView,
    Part,
)
from mlops_agents.agents.runtime.shielded import shielded
from mlops_agents.agents.runtime.store.base import StoreBase
from mlops_agents.clock import now_iso
from mlops_agents.ids import new_id
from mlops_agents.storage.tables import messages, turns

_PARTS_ADAPTER: TypeAdapter[list[Part]] = TypeAdapter(list[Part])


def _select_views() -> Select[Any]:
    """Message rows with their turn's error kind, which a failed turn's agent message shows."""
    return select(messages, turns.c.error_kind).select_from(
        messages.outerjoin(turns, turns.c.id == messages.c.turn_id)
    )


def _view(row: RowMapping) -> MessageView:
    author_kind = cast(AuthorKind, row["author_kind"])
    error_kind = cast(ErrorKind | None, row["error_kind"]) if author_kind == "agent" else None
    return MessageView(
        id=str(row["id"]),
        turn_id=cast(str | None, row["turn_id"]),
        author_kind=author_kind,
        author_name=cast(str, row["author_name"]),
        from_agent=cast(str | None, row["from_agent"]),
        parts=_PARTS_ADAPTER.validate_json(cast(str, row["parts"])),
        status=cast(MessageStatus, row["status"]),
        created_at=cast(str, row["created_at"]),
        error_kind=error_kind,
    )


class MessagesStore(StoreBase):
    """The messages table."""

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
        read_by_turn: str | None = None,
    ) -> MessageView:
        """Update parts, status and/or the turn that read it, of an existing message."""
        engine = await self._databases.project(project_id)
        values: dict[str, Any] = {}
        if parts is not None:
            values["parts"] = _PARTS_ADAPTER.dump_json(list(parts)).decode("utf-8")
        if status is not None:
            values["status"] = status
        if read_by_turn is not None:
            values["read_by_turn"] = read_by_turn

        async with engine.begin() as conn:
            if values:
                await conn.execute(
                    update(messages).where(messages.c.id == message_id).values(**values)
                )
            row = (
                (await conn.execute(_select_views().where(messages.c.id == message_id)))
                .mappings()
                .first()
            )

        if row is None:
            raise ValueError(f"Message {message_id} not found in project {project_id}")
        return _view(row)

    async def message(
        self, project_id: str, session_id: str, message_id: str
    ) -> MessageView | None:
        """Return one message of session_id, or None; a message of another session is None."""
        engine = await self._databases.project(project_id)
        query = _select_views().where(
            messages.c.id == message_id, messages.c.session_id == session_id
        )
        async with engine.connect() as conn:
            row = (await conn.execute(query)).mappings().first()
        return None if row is None else _view(row)

    async def turn_agent_message(
        self, project_id: str, session_id: str, turn_id: str
    ) -> MessageView | None:
        """Return the agent's message of a turn of session_id, or None when it wrote none."""
        engine = await self._databases.project(project_id)
        query = (
            _select_views()
            .where(
                messages.c.session_id == session_id,
                messages.c.turn_id == turn_id,
                messages.c.author_kind == "agent",
            )
            .order_by(messages.c.seq.desc())
        )
        async with engine.connect() as conn:
            row = (await conn.execute(query)).mappings().first()
        return None if row is None else _view(row)

    async def messages(
        self, project_id: str, session_id: str, *, after: str | None = None
    ) -> list[MessageView]:
        """Return messages for session_id in ascending seq order, optionally after a message id."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            query = (
                _select_views()
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

        return [_view(row) for row in rows]

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
