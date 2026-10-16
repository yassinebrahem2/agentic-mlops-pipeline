from pathlib import Path

from sqlalchemy import text

from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases


async def test_app_database_is_migrated_with_pragmas(tmp_path: Path) -> None:
    dbs = Databases(DataPaths(tmp_path))
    engine = await dbs.app()
    async with engine.connect() as conn:
        assert (await conn.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
        assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar() == 1
        assert (await conn.execute(text("PRAGMA busy_timeout"))).scalar() == 5000
        names = {r[0] for r in await conn.execute(text("SELECT name FROM sqlite_master"))}
    assert "projects" in names
    await dbs.dispose()


async def test_project_engine_is_cached_and_closable(tmp_path: Path) -> None:
    dbs = Databases(DataPaths(tmp_path))
    first = await dbs.project("prj_x")
    assert await dbs.project("prj_x") is first
    assert (tmp_path / "projects" / "prj_x" / "project.db").is_file()
    await dbs.close_project("prj_x")
    assert await dbs.project("prj_x") is not first
    await dbs.dispose()
