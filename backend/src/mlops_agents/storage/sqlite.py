from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
)


def create_engine_for(path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")

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
    conn = sqlite3.connect(path, isolation_level=None)
    for pragma in PRAGMAS:
        conn.execute(pragma)
    return conn


async def connect_checkpointer(path: Path) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(path)
    for pragma in PRAGMAS:
        await conn.execute(pragma)
    return conn
