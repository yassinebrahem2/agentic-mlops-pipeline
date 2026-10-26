from __future__ import annotations

from typing import cast

from pydantic import TypeAdapter
from sqlalchemy import RowMapping, delete, insert, select, update

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
from mlops_agents.storage.tables import messages, turns

_PARTS_ADAPTER: TypeAdapter[list[Part]] = TypeAdapter(list[Part])


def _view(row: RowMapping) -> MessageView:
    return MessageView(
        id=str(row["id"]),
        turn_id=cast(str | None, row["turn_id"]),
        author_kind=cast(AuthorKind, row["author_kind"]),
        author_name=cast(str, row["author_name"]),
        from_agent=cast(str | None, row["from_agent"]),
        parts=_PARTS_ADAPTER.validate_json(cast(str, row["parts"])),
        status=cast(MessageStatus, row["status"]),
        created_at=cast(str, row["created_at"]),
    )


class InboundStore(StoreBase):
    """Queries for mid-turn delivery and re-delivery."""

    @shielded
    async def take_inbound(
        self, project_id: str, session_id: str, turn_id: str
    ) -> list[MessageView]:
        """Select queued messages from agents, mark them complete read by turn_id, delete their
        triggered queued turns, and return the updated messages in seq order."""
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            stmt = (
                select(messages)
                .where(
                    messages.c.session_id == session_id,
                    messages.c.status == "queued",
                    messages.c.from_agent.is_not(None),
                )
                .order_by(messages.c.seq.asc())
            )
            rows = (await conn.execute(stmt)).mappings().all()
            if not rows:
                return []

            msg_ids = [str(r["id"]) for r in rows]

            await conn.execute(
                update(messages)
                .where(messages.c.id.in_(msg_ids))
                .values(status="complete", read_by_turn=turn_id)
            )

            await conn.execute(
                delete(turns).where(
                    turns.c.session_id == session_id,
                    turns.c.status == "queued",
                    turns.c.trigger_message_id.in_(msg_ids),
                )
            )

            updated_rows = (
                (
                    await conn.execute(
                        select(messages)
                        .where(messages.c.id.in_(msg_ids))
                        .order_by(messages.c.seq.asc())
                    )
                )
                .mappings()
                .all()
            )
            return [_view(r) for r in updated_rows]

    @shielded
    async def redeliver_inbound(
        self, project_id: str, session_id: str, turn_id: str
    ) -> list[MessageView]:
        """For a turn that did not end done, find messages taken mid-turn (read_by_turn == turn_id
        and not the trigger message), set them queued again with read_by_turn cleared, and insert
        a new queued turn for each."""
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            turn_row = (
                (
                    await conn.execute(
                        select(turns.c.trigger_message_id).where(turns.c.id == turn_id)
                    )
                )
                .mappings()
                .first()
            )
            trigger_msg_id = None if turn_row is None else str(turn_row["trigger_message_id"])

            stmt = select(messages).where(
                messages.c.session_id == session_id,
                messages.c.read_by_turn == turn_id,
            )
            if trigger_msg_id is not None:
                stmt = stmt.where(messages.c.id != trigger_msg_id)

            rows = (await conn.execute(stmt)).mappings().all()
            if not rows:
                return []

            msg_ids = [str(r["id"]) for r in rows]

            await conn.execute(
                update(messages)
                .where(messages.c.id.in_(msg_ids))
                .values(status="queued", read_by_turn=None)
            )

            now = now_iso()
            for r in rows:
                tid = new_id("trn")
                msg_created = cast(str | None, r["created_at"]) or now
                await conn.execute(
                    insert(turns).values(
                        id=tid,
                        session_id=session_id,
                        trigger_message_id=str(r["id"]),
                        status="queued",
                        error=None,
                        created_at=msg_created,
                        started_at=None,
                        ended_at=None,
                    )
                )

            updated_rows = (
                (
                    await conn.execute(
                        select(messages)
                        .where(messages.c.id.in_(msg_ids))
                        .order_by(messages.c.seq.asc())
                    )
                )
                .mappings()
                .all()
            )
            return [_view(r) for r in updated_rows]
