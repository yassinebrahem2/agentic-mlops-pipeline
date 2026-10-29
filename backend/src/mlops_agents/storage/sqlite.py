from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite
from sqlalchemy import URL, event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

MIN_SQLITE_VERSION: tuple[int, int, int] = (3, 35, 0)


def check_sqlite_version() -> None:
    """Refuse to run if SQLite is older than 3.35 (required for UPDATE ... RETURNING)."""
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        min_ver = ".".join(map(str, MIN_SQLITE_VERSION))
        found_ver = ".".join(map(str, sqlite3.sqlite_version_info))
        raise RuntimeError(f"SQLite {min_ver} or later is required, but found {found_ver}")


PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
)


def create_engine_for(path: Path) -> AsyncEngine:
    check_sqlite_version()
    url = URL.create("sqlite+aiosqlite", database=str(path))
    engine = create_async_engine(url)

    @event.listens_for(engine.sync_engine, "connect")
    def on_connect(dbapi_connection: Any, connection_record: Any) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()
        for pragma in PRAGMAS:
            cursor.execute(pragma)
        cursor.close()

    return engine


def connect_sync(path: Path) -> sqlite3.Connection:
    """A stdlib connection in autocommit mode: callers manage transactions explicitly."""
    check_sqlite_version()
    conn = sqlite3.connect(path, isolation_level=None)
    for pragma in PRAGMAS:
        conn.execute(pragma)
    return conn


async def connect_checkpointer(path: Path) -> aiosqlite.Connection:
    check_sqlite_version()
    conn = await aiosqlite.connect(path)
    for pragma in PRAGMAS:
        await conn.execute(pragma)
    return conn
