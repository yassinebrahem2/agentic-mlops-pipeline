"""Loading the sqlite-vec extension into SQLite connections."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import sqlite_vec  # pyright: ignore[reportMissingTypeStubs] - third-party package without stubs


def _check_extension_file(path_str: str) -> None:
    p = Path(path_str)
    candidates = (
        p,
        p.with_suffix(".dll"),
        p.with_suffix(".so"),
        p.with_suffix(".dylib"),
    )
    if not any(c.is_file() for c in candidates):
        raise FileNotFoundError(f"sqlite-vec extension library not found at {path_str}")


async def load_vector_extension(conn: aiosqlite.Connection) -> None:
    """Load the sqlite-vec extension into an aiosqlite connection."""
    path = sqlite_vec.loadable_path()
    _check_extension_file(path)
    await conn.enable_load_extension(True)
    try:
        await conn.load_extension(path)
    finally:
        await conn.enable_load_extension(False)  # never leave extension loading on
