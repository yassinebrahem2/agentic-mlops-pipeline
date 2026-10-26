"""Tests for permission settings storage."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mlops_agents.agents.approvals.settings import (
    DEFAULT_PERMISSION_LEVEL,
    PERMISSION_LEVEL_KEY,
    get_permission_level,
    set_permission_level,
)
from mlops_agents.agents.policy.table import PermissionLevel
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import project_settings


@pytest.mark.asyncio
async def test_permission_level_default(databases: Databases, paths: DataPaths) -> None:
    project_id = "prj_testdefault"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    level = await get_permission_level(databases, project_id)
    assert level == "ask"
    assert level == DEFAULT_PERMISSION_LEVEL


@pytest.mark.asyncio
async def test_permission_level_round_trip(databases: Databases, paths: DataPaths) -> None:
    project_id = "prj_testroundtrip"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    levels: list[PermissionLevel] = ["suggest", "ask", "autonomous", "bypass"]
    for level in levels:
        saved = await set_permission_level(databases, project_id, level)
        assert saved == level
        loaded = await get_permission_level(databases, project_id)
        assert loaded == level

        # Verify directly in project_settings table
        engine = await databases.project(project_id)
        async with engine.connect() as conn:
            val = (
                await conn.execute(
                    select(project_settings.c.value).where(
                        project_settings.c.key == PERMISSION_LEVEL_KEY
                    )
                )
            ).scalar_one()
            assert val == level


@pytest.mark.asyncio
async def test_permission_level_invalid_raises(databases: Databases, paths: DataPaths) -> None:
    project_id = "prj_testinvalid"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError):
        await set_permission_level(databases, project_id, "unknown_level")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_permission_level_unknown_in_db_falls_back_to_default(
    databases: Databases, paths: DataPaths
) -> None:
    project_id = "prj_testfallback"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            sqlite_insert(project_settings).values(key=PERMISSION_LEVEL_KEY, value="corrupt_value")
        )

    level = await get_permission_level(databases, project_id)
    assert level == "ask"
