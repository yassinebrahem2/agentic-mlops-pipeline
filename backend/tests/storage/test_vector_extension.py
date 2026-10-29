from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from mlops_agents.storage.databases import Databases
from mlops_agents.storage.sqlite import create_engine_for
from mlops_agents.storage.vector_extension import load_vector_extension


async def test_extension_loads_when_vectors_true(tmp_path: Path) -> None:
    db_path = tmp_path / "vec_true.db"
    engine = create_engine_for(db_path, vectors=True)
    async with engine.connect() as conn:
        res = await conn.execute(text("SELECT vec_version()"))
        version = res.scalar()
        assert isinstance(version, str)
        assert version.startswith("v")
    await engine.dispose()


async def test_load_vector_extension_direct(tmp_path: Path) -> None:
    db_path = tmp_path / "direct.db"
    conn = await aiosqlite.connect(db_path)
    try:
        await load_vector_extension(conn)
        cursor = await conn.execute("SELECT vec_version()")
        row = await cursor.fetchone()
        assert row is not None and str(row[0]).startswith("v")
    finally:
        await conn.close()


async def test_extension_not_loaded_by_default(tmp_path: Path) -> None:
    db_path = tmp_path / "vec_default.db"
    engine = create_engine_for(db_path)
    async with engine.connect() as conn:
        with pytest.raises((OperationalError, DBAPIError, sqlite3.OperationalError)) as exc_info:
            await conn.execute(text("SELECT vec_version()"))
        assert "no such function: vec_version" in str(exc_info.value).lower()
    await engine.dispose()


async def test_databases_project_has_extension_app_does_not(databases: Databases) -> None:
    project_engine = await databases.project("prj_vectors1")
    async with project_engine.connect() as conn:
        res = await conn.execute(text("SELECT vec_version()"))
        version = res.scalar()
        assert isinstance(version, str)
        assert version.startswith("v")

    app_engine = await databases.app()
    async with app_engine.connect() as conn:
        with pytest.raises((OperationalError, DBAPIError, sqlite3.OperationalError)) as exc_info:
            await conn.execute(text("SELECT vec_version()"))
        assert "no such function: vec_version" in str(exc_info.value).lower()


async def test_extension_loads_with_path_with_spaces(tmp_path: Path) -> None:
    spaced_folder = tmp_path / "path with spaces #1"
    spaced_folder.mkdir(parents=True, exist_ok=True)
    db_path = spaced_folder / "test spaces.db"
    engine = create_engine_for(db_path, vectors=True)
    async with engine.connect() as conn:
        res = await conn.execute(text("SELECT vec_version()"))
        version = res.scalar()
        assert isinstance(version, str)
        assert version.startswith("v")
    await engine.dispose()


async def test_extension_works_on_every_connection_of_pool(tmp_path: Path) -> None:
    db_path = tmp_path / "vec_pool.db"
    engine = create_engine_for(db_path, vectors=True)
    async with engine.connect() as conn1, engine.connect() as conn2:
        res1 = await conn1.execute(text("SELECT vec_version()"))
        res2 = await conn2.execute(text("SELECT vec_version()"))
        v1 = res1.scalar()
        v2 = res2.scalar()
        assert isinstance(v1, str) and v1.startswith("v")
        assert isinstance(v2, str) and v2.startswith("v")
    await engine.dispose()


async def test_missing_extension_graceful_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "mlops_agents.storage.vector_extension.sqlite_vec.loadable_path",
        lambda: str(tmp_path / "nonexistent_vec0"),
    )
    db_path = tmp_path / "fallback.db"
    # create_engine_for must not crash
    engine = create_engine_for(db_path, vectors=True)
    # The connection still succeeds
    async with engine.connect() as conn:
        res = await conn.execute(text("SELECT 1"))
        assert res.scalar() == 1
        with pytest.raises((OperationalError, DBAPIError, sqlite3.OperationalError)):
            await conn.execute(text("SELECT vec_version()"))
    await engine.dispose()
