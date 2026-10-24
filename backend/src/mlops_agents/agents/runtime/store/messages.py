from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from pydantic import TypeAdapter
from sqlalchemy import and_, insert, select, update

from mlops_agents.agents.runtime.contract import (
    AuthorKind,
    MessageStatus,
    MessageView,
    Part,
)
from mlops_agents.agents.runtime.shielded import shielded
from mlops_agents.agents.runtime.store.base import StoreBase
from mlops_agents.clock import now_iso
from mlops_agents.ids import new_id
from mlops_agents.storage.tables import messages

_PARTS_ADAPTER: TypeAdapter[list[Part]] = TypeAdapter(list[Part])


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
