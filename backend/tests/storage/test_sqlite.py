from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from mlops_agents.storage.sqlite import (
    MIN_SQLITE_VERSION,
    check_sqlite_version,
    connect_checkpointer,
    connect_sync,
    create_engine_for,
)


def test_sqlite_version_floor_passes_on_supported_version() -> None:
    # Real environment version must satisfy the floor.
    assert sqlite3.sqlite_version_info >= MIN_SQLITE_VERSION
    check_sqlite_version()


def test_sqlite_version_floor_raises_clear_error_when_below_minimum() -> None:
    with (
        patch.object(sqlite3, "sqlite_version_info", (3, 34, 0)),
        pytest.raises(RuntimeError) as exc_info,
    ):
        check_sqlite_version()
    err_msg = str(exc_info.value)
    assert "3.35.0" in err_msg or "3.35" in err_msg
    assert "3.34.0" in err_msg
    assert "required" in err_msg.lower()


def test_connect_sync_enforces_sqlite_version_floor(tmp_path: Path) -> None:
    db_file = tmp_path / "test.db"
    with (
        patch.object(sqlite3, "sqlite_version_info", (3, 34, 0)),
        pytest.raises(RuntimeError, match=r"3\.35"),
    ):
        connect_sync(db_file)


def test_create_engine_enforces_sqlite_version_floor(tmp_path: Path) -> None:
    db_file = tmp_path / "test.db"
    with (
        patch.object(sqlite3, "sqlite_version_info", (3, 34, 0)),
        pytest.raises(RuntimeError, match=r"3\.35"),
    ):
        create_engine_for(db_file)


async def test_connect_checkpointer_enforces_sqlite_version_floor(tmp_path: Path) -> None:
    db_file = tmp_path / "test.db"
    with (
        patch.object(sqlite3, "sqlite_version_info", (3, 34, 0)),
        pytest.raises(RuntimeError, match=r"3\.35"),
    ):
        await connect_checkpointer(db_file)
