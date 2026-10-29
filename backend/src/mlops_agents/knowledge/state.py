"""Knowledge state settings over project_settings (I17)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import project_settings

KNOWLEDGE_VECTORS_READY_KEY = "knowledge_vectors_ready_for"


async def get_vectors_ready_for(databases: Databases, project_id: str) -> str | None:
    """Reads the project's vector ready key from project_settings; returns None if not set."""
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(project_settings.c.value).where(
                    project_settings.c.key == KNOWLEDGE_VECTORS_READY_KEY
                )
            )
        ).scalar_one_or_none()
    return str(row) if row is not None else None


async def set_vectors_ready_for(databases: Databases, project_id: str, key: str) -> None:
    """Sets the project's vector ready key in project_settings."""
    engine = await databases.project(project_id)
    stmt = (
        sqlite_insert(project_settings)
        .values(key=KNOWLEDGE_VECTORS_READY_KEY, value=key)
        .on_conflict_do_update(
            index_elements=["key"],
            set_={"value": key},
        )
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)
