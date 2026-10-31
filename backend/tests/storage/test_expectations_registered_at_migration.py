"""project.db 0008: an expectation's registered_at, backfilled for the rows a project already
has so that an upgrade raises no silence for slots before the upgrade."""

import re
import sqlite3
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import pytest

from mlops_agents.clock import to_iso
from mlops_agents.storage.migrate import migrate
from mlops_agents.storage.sqlite import connect_sync

_SEEN = "2026-10-30T06:05:00.000Z"
_SIGNALLED = "2026-10-29T06:00:00.000Z"
_UP_TO_7 = [
    "0001_init.sql",
    "0002_stage2.sql",
    "0003_approvals_tool_call.sql",
    "0004_stage3.sql",
    "0005_drop_turn_error_kind.sql",
    "0006_approval_settling.sql",
    "0007_stage4_5.sql",
]
_INSERT = (
    "INSERT INTO expectations (kind, ref, model, spec, active, last_seen, "
    "last_signalled_first, last_signalled_second) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_REGISTERED = (
    "INSERT INTO expectations (kind, ref, model, spec, active, last_seen, "
    "last_signalled_first, last_signalled_second, registered_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_SELECT = (
    "SELECT kind, ref, model, spec, active, last_seen, last_signalled_first, "
    "last_signalled_second, registered_at FROM expectations ORDER BY kind"
)
_ROWS = [
    ("check", "chk_auc", "cancel", '{"a": 1}', 0, None, None, None),
    ("feed", "bookings", None, '{"b": 2}', 1, _SEEN, _SIGNALLED, _SIGNALLED),
]


def _version_7(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    for name in _UP_TO_7:
        sql = files("mlops_agents.storage").joinpath("migrations", "project", name)
        (source / name).write_text(sql.read_text("utf-8"), encoding="utf-8")
    db = tmp_path / "project.db"
    assert migrate(db, "project", tmp_path / "bk", source=source) == list(range(1, 8))
    return db


def test_a_version_7_database_backfills_registered_at_and_keeps_every_other_value(
    tmp_path: Path,
) -> None:
    db = _version_7(tmp_path)
    conn = connect_sync(db)
    try:
        conn.executemany(_INSERT, _ROWS)
    finally:
        conn.close()

    before = datetime.now(UTC).replace(microsecond=0)
    assert migrate(db, "project", tmp_path / "bk") == [8]
    after = datetime.now(UTC)

    conn = connect_sync(db)
    try:
        rows = conn.execute(_SELECT).fetchall()
        assert [row[:-1] for row in rows] == _ROWS
        for row in rows:
            registered_at = str(row[-1])
            assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", registered_at)
            parsed = datetime.fromisoformat(registered_at)
            assert to_iso(parsed) == registered_at
            assert before <= parsed <= after
        # The rebuilt unique index still refuses a second row for a key.
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(_INSERT_REGISTERED, (*_ROWS[1], _SEEN))
    finally:
        conn.close()


def test_a_fresh_database_requires_registered_at_last_and_keeps_its_checks(tmp_path: Path) -> None:
    db = tmp_path / "project.db"
    assert migrate(db, "project", tmp_path / "bk") == list(range(1, 9))
    conn = connect_sync(db)
    try:
        info = conn.execute("PRAGMA table_info(expectations)").fetchall()
        assert (info[-1][1], info[-1][3]) == ("registered_at", 1)
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            conn.execute(_INSERT, _ROWS[0])
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(_INSERT_REGISTERED, ("cron", *_ROWS[0][1:], _SEEN))
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(_INSERT_REGISTERED, (*_ROWS[0][:4], 2, *_ROWS[0][5:], _SEEN))
    finally:
        conn.close()
