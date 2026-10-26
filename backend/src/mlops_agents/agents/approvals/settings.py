"""Permission level settings for projects (I10)."""

from __future__ import annotations

from typing import cast, get_args

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.agents.policy.table import PermissionLevel
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import project_settings

DEFAULT_PERMISSION_LEVEL: PermissionLevel = "ask"
PERMISSION_LEVEL_KEY = "permission_level"


async def get_permission_level(databases: Databases, project_id: str) -> PermissionLevel:
    """Reads the project's permission level from its database; defaults to 'ask'."""
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(project_settings.c.value).where(
                    project_settings.c.key == PERMISSION_LEVEL_KEY
                )
            )
        ).scalar_one_or_none()
    if row is not None and row in get_args(PermissionLevel):
        return cast(PermissionLevel, row)
    return DEFAULT_PERMISSION_LEVEL


async def set_permission_level(
    databases: Databases, project_id: str, level: PermissionLevel
) -> PermissionLevel:
    """Sets the project's permission level in its database."""
    if level not in get_args(PermissionLevel):
        raise ValueError(
            f"Invalid permission level: {level!r}. Expected one of {get_args(PermissionLevel)}"
        )
    engine = await databases.project(project_id)
    stmt = (
        sqlite_insert(project_settings)
        .values(key=PERMISSION_LEVEL_KEY, value=level)
        .on_conflict_do_update(
            index_elements=["key"],
            set_={"value": level},
        )
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)
    return level
