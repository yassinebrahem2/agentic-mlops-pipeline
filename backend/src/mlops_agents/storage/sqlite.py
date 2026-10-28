from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite
from sqlalchemy import URL, event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mlops_agents.storage.vector_extension import load_vector_extension

logger = logging.getLogger(__name__)

PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
)


def create_engine_for(path: Path, *, vectors: bool = False) -> AsyncEngine:
    url = URL.create("sqlite+aiosqlite", database=str(path))
    engine = create_async_engine(url)

    @event.listens_for(engine.sync_engine, "connect")
    def on_connect(dbapi_connection: Any, connection_record: Any) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()
        for pragma in PRAGMAS:
            cursor.execute(pragma)
        cursor.close()
        if vectors:
            try:

                def _load_vec(_raw: object) -> Any:
                    return load_vector_extension(dbapi_connection.driver_connection)

                dbapi_connection.run_async(_load_vec)
            except (AttributeError, sqlite3.OperationalError, OSError) as exc:
                logger.warning(
                    "sqlite-vec extension failed to load (%s); vector features will be unavailable",
                    type(exc).__name__,
                )

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
