"""project.db 0006: who decided an approval, whether its records and card are written, and the
indexes the settle checks and the repair's unsettled query use."""

import sqlite3
from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path

import pytest

from mlops_agents.storage.migrate import migrate
from mlops_agents.storage.sqlite import connect_sync

_TS = "2026-10-28T00:00:00Z"
_DECIDED = "2026-10-28T00:05:00Z"
_UP_TO_5 = [
    "0001_init.sql",
    "0002_stage2.sql",
    "0003_approvals_tool_call.sql",
    "0004_stage3.sql",
    "0005_drop_turn_error_kind.sql",
]


def _version_5(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    for name in _UP_TO_5:
        sql = files("mlops_agents.storage").joinpath("migrations", "project", name)
        (source / name).write_text(sql.read_text("utf-8"), encoding="utf-8")
    db = tmp_path / "project.db"
    assert migrate(db, "project", tmp_path / "bk", source=source) == [1, 2, 3, 4, 5]
    return db


def _seed_turn(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO sessions (id, agent, created_at) VALUES ('ses_1', 'coder', ?)", (_TS,)
    )
    conn.execute(
        "INSERT INTO turns (id, session_id, trigger_message_id, status, created_at) "
        "VALUES ('trn_1', 'ses_1', 'msg_1', 'waiting', ?)",
        (_TS,),
    )


def _insert_approval(
    conn: sqlite3.Connection, approval_id: str, status: str, decided_at: str | None
) -> None:
    conn.execute(
        "INSERT INTO approvals (id, turn_id, session_id, message_id, agent, tool_call_id, tool, "
        "risk_class, preview, status, created_at, decided_at) "
        "VALUES (?, 'trn_1', 'ses_1', 'msg_1', 'coder', ?, 'Edit', 'write_code', '{}', ?, ?, ?)",
        (approval_id, f"call_{approval_id}", status, _TS, decided_at),
    )


@pytest.fixture
def fresh_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "project.db"
    assert migrate(db, "project", tmp_path / "bk") == [1, 2, 3, 4, 5, 6]
    conn = connect_sync(db)
    _seed_turn(conn)
    yield conn
    conn.close()


def test_a_fresh_database_has_the_two_columns_last_and_the_three_indexes(
    fresh_db: sqlite3.Connection,
) -> None:
    columns = [str(r[1]) for r in fresh_db.execute("PRAGMA table_info(approvals)")]
    assert columns[-2:] == ["decided_by", "settled_at"]
    indexes = {
        str(r[0]) for r in fresh_db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert {"approvals_unsettled", "events_type_subject", "audit_approval_id"} <= indexes


def test_a_version_5_database_settles_its_decided_approvals_only(tmp_path: Path) -> None:
    db = _version_5(tmp_path)
    conn = connect_sync(db)
    try:
        _seed_turn(conn)
        _insert_approval(conn, "apr_pending", "pending", None)
        _insert_approval(conn, "apr_approved", "approved", _DECIDED)
        _insert_approval(conn, "apr_expired", "expired", _DECIDED)
        _insert_approval(conn, "apr_no_time", "cancelled", None)
    finally:
        conn.close()

    assert migrate(db, "project", tmp_path / "bk") == [6]

    conn = connect_sync(db)
    try:
        rows = conn.execute(
            "SELECT id, settled_at, decided_by FROM approvals ORDER BY id"
        ).fetchall()
        assert rows == [
            ("apr_approved", _DECIDED, None),
            ("apr_expired", _DECIDED, None),
            ("apr_no_time", _TS, None),
            ("apr_pending", None, None),
        ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


@pytest.mark.parametrize("decided_by", [None, "user", "system"])
def test_decided_by_accepts_null_user_and_system(
    fresh_db: sqlite3.Connection, decided_by: str | None
) -> None:
    _insert_approval(fresh_db, "apr_1", "approved", _DECIDED)
    fresh_db.execute("UPDATE approvals SET decided_by = ? WHERE id = 'apr_1'", (decided_by,))
    fresh_db.execute(
        "INSERT INTO approvals (id, turn_id, session_id, message_id, agent, tool_call_id, tool, "
        "risk_class, preview, status, created_at, decided_by) VALUES ('apr_2', 'trn_1', 'ses_1', "
        "'msg_1', 'coder', 'call_2', 'Edit', 'write_code', '{}', 'expired', ?, ?)",
        (_TS, decided_by),
    )
    rows = fresh_db.execute("SELECT decided_by FROM approvals ORDER BY id").fetchall()
    assert rows == [(decided_by,), (decided_by,)]


def test_decided_by_refuses_any_other_value(fresh_db: sqlite3.Connection) -> None:
    _insert_approval(fresh_db, "apr_1", "approved", _DECIDED)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        fresh_db.execute("UPDATE approvals SET decided_by = 'agent' WHERE id = 'apr_1'")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        fresh_db.execute(
            "INSERT INTO approvals (id, turn_id, session_id, message_id, agent, tool_call_id, "
            "tool, risk_class, preview, status, created_at, decided_by) VALUES ('apr_2', "
            "'trn_1', 'ses_1', 'msg_1', 'coder', 'call_2', 'Edit', 'write_code', '{}', "
            "'approved', ?, 'User')",
            (_TS,),
        )


def _plan(conn: sqlite3.Connection, sql: str, params: tuple[str, ...] = ()) -> str:
    return "\n".join(str(r[3]) for r in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params))


def test_the_unsettled_query_and_the_two_settle_checks_use_their_indexes(
    fresh_db: sqlite3.Connection,
) -> None:
    unsettled = _plan(
        fresh_db,
        "SELECT id FROM approvals WHERE status <> 'pending' AND settled_at IS NULL "
        "ORDER BY created_at",
    )
    assert "approvals_unsettled" in unsettled
    event = _plan(
        fresh_db,
        "SELECT 1 FROM events WHERE type = 'approval.decided' AND subject_id = ? LIMIT 1",
        ("apr_1",),
    )
    assert "events_type_subject" in event
    assert "SCAN" not in event
    audit = _plan(
        fresh_db,
        "SELECT 1 FROM audit WHERE action = 'approval' "
        "AND json_extract(detail, '$.approval_id') = ? LIMIT 1",
        ("apr_1",),
    )
    assert "audit_approval_id" in audit
    assert "SCAN" not in audit
