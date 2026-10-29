import sqlite3
from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path

import pytest

from mlops_agents.storage.migrate import migrate
from mlops_agents.storage.sqlite import connect_sync

_V1_SQL = (
    files("mlops_agents.storage")
    .joinpath("migrations", "project", "0001_init.sql")
    .read_text("utf-8")
)
_TS = "2026-10-24T00:00:00Z"


def _names(conn: sqlite3.Connection, kind: str) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
    return {str(r[0]) for r in rows}


def _message_columns(conn: sqlite3.Connection) -> list[str]:
    return [str(r[1]) for r in conn.execute("PRAGMA table_info(messages)")]


def _insert_message(
    conn: sqlite3.Connection,
    msg_id: str,
    *,
    session: str = "ses_1",
    turn: str | None = None,
    status: str = "complete",
) -> int:
    cursor = conn.execute(
        "INSERT INTO messages "
        "(id, session_id, turn_id, author_kind, author_name, parts, status, created_at) "
        "VALUES (?, ?, ?, 'user', 'me', '[]', ?, ?)",
        (msg_id, session, turn, status, _TS),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _v1_database(tmp_path: Path) -> Path:
    """A project.db at schema version 1 with a gap in messages.seq, as an old install has."""
    source = tmp_path / "v1"
    source.mkdir()
    (source / "0001_init.sql").write_text(_V1_SQL, encoding="utf-8")
    db = tmp_path / "project.db"
    assert migrate(db, "project", tmp_path / "bk", source=source) == [1]
    conn = connect_sync(db)
    try:
        conn.execute(
            "INSERT INTO sessions (id, agent, created_at) VALUES ('ses_1', 'coder', ?)", (_TS,)
        )
        conn.execute(
            "INSERT INTO turns (id, session_id, trigger_message_id, status, created_at) "
            "VALUES ('trn_1', 'ses_1', 'msg_1', 'done', ?)",
            (_TS,),
        )
        for index in range(1, 5):
            _insert_message(conn, f"msg_{index}", turn="trn_1" if index == 2 else None)
        conn.execute("DELETE FROM messages WHERE id = 'msg_2'")  # seq 2 is now a gap
        conn.execute("DELETE FROM messages WHERE id = 'msg_4'")  # the high-water mark outlives it
    finally:
        conn.close()
    return db


def test_version_1_database_migrates_to_2_keeping_every_row_and_seq(tmp_path: Path) -> None:
    db = _v1_database(tmp_path)
    conn = connect_sync(db)
    before = conn.execute("SELECT * FROM messages ORDER BY seq").fetchall()
    conn.close()
    assert [row[0] for row in before] == [1, 3]

    assert migrate(db, "project", tmp_path / "bk") == [2, 3, 4, 5, 6, 7]

    conn = connect_sync(db)
    try:
        after = conn.execute("SELECT * FROM messages ORDER BY seq").fetchall()
        assert [row[:10] for row in after] == before
        assert [row[10:] for row in after] == [(None, None, None)] * 2
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert "messages_session_seq" in _names(conn, "index")
        assert _names(conn, "table").isdisjoint({"messages_new"})
        # The AUTOINCREMENT high-water mark survives: seq 4 is never handed out again.
        assert _insert_message(conn, "msg_5") == 5
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM turns").fetchone() == (1,)
    finally:
        conn.close()


def test_backup_is_taken_first_and_still_has_the_version_1_schema(tmp_path: Path) -> None:
    db = _v1_database(tmp_path)
    migrate(db, "project", tmp_path / "bk")
    backups = list((tmp_path / "bk").glob("*.db"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        assert "tasks" not in _names(backup, "table")
        assert "exchange_id" not in _message_columns(backup)
        assert backup.execute("SELECT COUNT(*) FROM messages").fetchone() == (2,)
    finally:
        backup.close()


def test_fresh_database_gets_both_migrations(tmp_path: Path) -> None:
    db = tmp_path / "project.db"
    assert migrate(db, "project", tmp_path / "bk") == [1, 2, 3, 4, 5, 6, 7]
    conn = connect_sync(db)
    try:
        assert {
            "tasks",
            "task_reads",
            "file_claims",
            "approvals",
            "events",
            "audit",
            "exchanges",
            "wakeups",
        } <= _names(conn, "table")
        assert _message_columns(conn)[-3:] == ["exchange_id", "hop", "read_by_turn"]
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone() == (7,)
    finally:
        conn.close()


def test_indexes_exist(tmp_path: Path) -> None:
    db = tmp_path / "project.db"
    migrate(db, "project", tmp_path / "bk")
    conn = connect_sync(db)
    try:
        assert {
            "tasks_session_status",
            "file_claims_path",
            "approvals_status",
            "approvals_turn_id",
            "wakeups_status_due",
            "messages_session_seq",
            "messages_exchange",
            "messages_read_by_turn",
            "events_type",
        } <= _names(conn, "index")
    finally:
        conn.close()


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "project.db"
    migrate(db, "project", tmp_path / "bk")
    connection = connect_sync(db)
    connection.execute(
        "INSERT INTO sessions (id, agent, created_at) VALUES ('ses_1', 'coder', ?)", (_TS,)
    )
    connection.execute(
        "INSERT INTO turns (id, session_id, trigger_message_id, status, created_at) "
        "VALUES ('trn_1', 'ses_1', 'msg_1', 'running', ?)",
        (_TS,),
    )
    yield connection
    connection.close()


def _task(conn: sqlite3.Connection, task_id: str = "tsk_1", **overrides: object) -> None:
    values: dict[str, object] = {
        "id": task_id,
        "session_id": "ses_1",
        "agent": "coder",
        "title": "Fix it",
        "branch": f"agent/coder/fix-{task_id}",
        "worktree": f"worktrees/{task_id}",
        "base_ref": "main",
        "base_sha": "a" * 40,
        "status": "active",
        "review_kind": None,
        "pr_number": None,
        "pr_url": None,
        "created_at": _TS,
        "ended_at": None,
    }
    values.update(overrides)
    conn.execute(
        "INSERT INTO tasks VALUES (:id, :session_id, :agent, :title, :branch, :worktree, "
        ":base_ref, :base_sha, :status, :review_kind, :pr_number, :pr_url, :created_at, :ended_at)",
        values,
    )


def _approval(conn: sqlite3.Connection, approval_id: str = "apr_1", **overrides: object) -> None:
    values: dict[str, object] = {
        "id": approval_id,
        "turn_id": "trn_1",
        "session_id": "ses_1",
        "message_id": "msg_1",
        "agent": "coder",
        "tool_call_id": f"call_{approval_id}",
        "tool": "Bash",
        "risk_class": "run",
        "preview": "{}",
        "status": "pending",
        "note": None,
        "created_at": _TS,
        "decided_at": None,
        "task_id": None,
    }
    values.update(overrides)
    conn.execute(
        "INSERT INTO approvals (id, turn_id, session_id, message_id, agent, tool_call_id, tool, "
        "risk_class, preview, status, note, created_at, decided_at, task_id) "
        "VALUES (:id, :turn_id, :session_id, :message_id, :agent, "
        ":tool_call_id, :tool, :risk_class, :preview, :status, :note, :created_at, :decided_at, :task_id)",
        values,
    )


def _wakeup(conn: sqlite3.Connection, **overrides: object) -> None:
    values: dict[str, object] = {
        "id": "wak_1",
        "session_id": "ses_1",
        "agent": "coder",
        "note": "look again",
        "due_at": _TS,
        "status": "pending",
        "created_at": _TS,
        "fired_at": None,
    }
    values.update(overrides)
    conn.execute(
        "INSERT INTO wakeups VALUES (:id, :session_id, :agent, :note, :due_at, :status, "
        ":created_at, :fired_at)",
        values,
    )


def test_valid_rows_are_accepted(conn: sqlite3.Connection) -> None:
    _task(conn, review_kind=None)
    _task(conn, "tsk_2", review_kind="github", pr_number=7, pr_url="https://x/7")
    _task(conn, "tsk_3", review_kind="local", status="discarded", ended_at=_TS)
    _approval(conn)
    _wakeup(conn)
    _insert_message(conn, "msg_w", status="waiting")


@pytest.mark.parametrize("status", ["done", "", "ACTIVE"])
def test_tasks_status_check(conn: sqlite3.Connection, status: str) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _task(conn, status=status)


def test_tasks_review_kind_check(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _task(conn, review_kind="gitlab")


def test_approvals_status_check(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _approval(conn, status="maybe")


def test_wakeups_status_check(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _wakeup(conn, status="done")


def test_messages_status_check_accepts_waiting_and_rejects_others(conn: sqlite3.Connection) -> None:
    _insert_message(conn, "msg_w", status="waiting")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert_message(conn, "msg_bad", status="paused")


def test_messages_author_kind_check_survives_the_rebuild(conn: sqlite3.Connection) -> None:
    for kind in ("user", "agent", "system"):
        conn.execute(
            "INSERT INTO messages (id, session_id, author_kind, author_name, parts, status, "
            "created_at) VALUES (?, 'ses_1', ?, 'x', '[]', 'complete', ?)",
            (f"msg_{kind}", kind, _TS),
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO messages (id, session_id, author_kind, author_name, parts, status, "
            "created_at) VALUES ('msg_bad', 'ses_1', 'robot', 'x', '[]', 'complete', ?)",
            (_TS,),
        )


_AUDIT_COLUMNS = (
    "id",
    "ts",
    "actor",
    "action",
    "risk_class",
    "permission_level",
    "decision",
    "target",
    "detail",
)


def _audit(conn: sqlite3.Connection, **overrides: object) -> None:
    values: dict[str, object] = {
        "id": "aud_1",
        "ts": _TS,
        "actor": "coder",
        "action": "tool.run",
        "risk_class": "run",
        "permission_level": "ask",
        "decision": "allowed",
        "target": "Bash",
        "detail": "{}",
    }
    values.update(overrides)
    conn.execute(
        "INSERT INTO audit VALUES (:id, :ts, :actor, :action, :risk_class, :permission_level, "
        ":decision, :target, :detail)",
        values,
    )


def test_audit_accepts_a_complete_row(conn: sqlite3.Connection) -> None:
    _audit(conn)


@pytest.mark.parametrize("column", _AUDIT_COLUMNS)
def test_audit_rejects_a_null_in_every_column(conn: sqlite3.Connection, column: str) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        _audit(conn, **{column: None})


def test_approvals_tool_call_id_allows_duplicates(conn: sqlite3.Connection) -> None:
    _approval(conn)
    _approval(conn, "apr_2", tool_call_id="call_apr_1")
    rows = conn.execute(
        "SELECT id FROM approvals WHERE tool_call_id = 'call_apr_1' ORDER BY id"
    ).fetchall()
    assert rows == [("apr_1",), ("apr_2",)]


def test_events_seq_autoincrements_and_id_is_unique(conn: sqlite3.Connection) -> None:
    sql = "INSERT INTO events (id, ts, source, type, payload) VALUES (?, ?, 'system', 'x', '{}')"
    first = conn.execute(sql, ("evt_1", _TS)).lastrowid
    second = conn.execute(sql, ("evt_2", _TS)).lastrowid
    assert (first, second) == (1, 2)
    conn.execute("DELETE FROM events WHERE id = 'evt_2'")
    assert conn.execute(sql, ("evt_3", _TS)).lastrowid == 3  # AUTOINCREMENT: never reused
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute(sql, ("evt_1", _TS))


def test_foreign_keys_are_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _task(conn, session_id="ses_missing")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _approval(conn, turn_id="trn_missing")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute("INSERT INTO task_reads (task_id, path, sha256) VALUES ('tsk_x', 'a', 'h')")


def test_deleting_a_session_cascades_to_tasks_and_wakeups(conn: sqlite3.Connection) -> None:
    _task(conn)
    conn.execute("INSERT INTO task_reads (task_id, path, sha256) VALUES ('tsk_1', 'a.py', 'h')")
    conn.execute("INSERT INTO file_claims (task_id, path) VALUES ('tsk_1', 'a.py')")
    _wakeup(conn)
    conn.execute("DELETE FROM sessions WHERE id = 'ses_1'")
    counts = conn.execute(
        "SELECT (SELECT COUNT(*) FROM tasks), (SELECT COUNT(*) FROM task_reads), "
        "(SELECT COUNT(*) FROM file_claims), (SELECT COUNT(*) FROM wakeups), "
        "(SELECT COUNT(*) FROM turns)"
    ).fetchone()
    assert counts == (0, 0, 0, 0, 0)


def test_deleting_a_task_cascades_to_reads_and_claims(conn: sqlite3.Connection) -> None:
    _task(conn)
    _task(conn, "tsk_2")
    for task in ("tsk_1", "tsk_2"):
        conn.execute(
            "INSERT INTO task_reads (task_id, path, sha256) VALUES (?, 'a.py', 'h')", (task,)
        )
        conn.execute("INSERT INTO file_claims (task_id, path) VALUES (?, 'a.py')", (task,))
    conn.execute("DELETE FROM tasks WHERE id = 'tsk_1'")
    assert conn.execute("SELECT task_id FROM task_reads").fetchall() == [("tsk_2",)]
    assert conn.execute("SELECT task_id FROM file_claims").fetchall() == [("tsk_2",)]


def test_task_reads_and_claims_primary_keys(conn: sqlite3.Connection) -> None:
    _task(conn)
    conn.execute("INSERT INTO file_claims (task_id, path) VALUES ('tsk_1', 'a.py')")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute("INSERT INTO file_claims (task_id, path) VALUES ('tsk_1', 'a.py')")


def test_deleting_a_turn_cascades_to_approvals_and_nulls_read_by_turn(
    conn: sqlite3.Connection,
) -> None:
    _approval(conn)
    seq = _insert_message(conn, "msg_in")
    conn.execute(
        "UPDATE messages SET read_by_turn = 'trn_1', turn_id = 'trn_1' WHERE seq = ?", (seq,)
    )
    conn.execute("DELETE FROM turns WHERE id = 'trn_1'")
    assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone() == (0,)
    assert conn.execute("SELECT turn_id, read_by_turn FROM messages").fetchall() == [(None, None)]


def test_deleting_a_session_removes_its_messages(conn: sqlite3.Connection) -> None:
    _insert_message(conn, "msg_in")
    conn.execute("DELETE FROM sessions WHERE id = 'ses_1'")
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)


def _index_columns(conn: sqlite3.Connection, table: str, name: str) -> list[str]:
    assert any(row[1] == name for row in conn.execute(f"PRAGMA index_list({table})"))
    return [str(row[2]) for row in conn.execute(f"PRAGMA index_info({name})")]


def test_messages_exchange_columns_are_indexed(conn: sqlite3.Connection) -> None:
    assert _index_columns(conn, "messages", "messages_exchange") == ["exchange_id"]
    assert _index_columns(conn, "messages", "messages_read_by_turn") == ["read_by_turn"]


def test_events_type_is_indexed(conn: sqlite3.Connection) -> None:
    assert _index_columns(conn, "events", "events_type") == ["type"]


def test_messages_exchange_id_and_read_by_turn_are_set_null_foreign_keys(
    conn: sqlite3.Connection,
) -> None:
    foreign_keys = {
        str(row[3]): (str(row[2]), str(row[4]), str(row[6]))
        for row in conn.execute("PRAGMA foreign_key_list(messages)")
    }
    assert foreign_keys["exchange_id"] == ("exchanges", "id", "SET NULL")
    assert foreign_keys["read_by_turn"] == ("turns", "id", "SET NULL")


def test_deleting_an_exchange_keeps_its_messages_with_a_null_exchange(
    conn: sqlite3.Connection,
) -> None:
    conn.execute("INSERT INTO exchanges (id, created_at) VALUES ('xch_1', ?)", (_TS,))
    seq = _insert_message(conn, "msg_in")
    conn.execute("UPDATE messages SET exchange_id = 'xch_1', hop = 1 WHERE seq = ?", (seq,))
    conn.execute("DELETE FROM exchanges WHERE id = 'xch_1'")
    assert conn.execute("SELECT id, exchange_id, hop FROM messages").fetchall() == [
        ("msg_in", None, 1)
    ]


def test_messages_exchange_id_must_name_an_exchange(conn: sqlite3.Connection) -> None:
    seq = _insert_message(conn, "msg_in")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute("UPDATE messages SET exchange_id = 'xch_missing' WHERE seq = ?", (seq,))
