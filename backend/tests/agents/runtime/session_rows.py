"""Reading and writing project.db rows directly, for the session tests."""

from __future__ import annotations

from sqlalchemy import RowMapping, insert, select

from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import exchanges, messages


async def message_row(databases: Databases, project_id: str, message_id: str) -> RowMapping:
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        row = (
            (await conn.execute(select(messages).where(messages.c.id == message_id)))
            .mappings()
            .one()
        )
    return row


async def create_exchange(databases: Databases, project_id: str, exchange_id: str) -> None:
    """Store the exchange a posted message continues: messages.exchange_id references it."""
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            insert(exchanges).values(id=exchange_id, created_at="2026-10-24T00:00:00Z")
        )
