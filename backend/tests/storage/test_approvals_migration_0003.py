from importlib.resources import files
from pathlib import Path

from mlops_agents.storage.migrate import migrate
from mlops_agents.storage.sqlite import connect_sync

_V1_SQL = (
    files("mlops_agents.storage")
    .joinpath("migrations", "project", "0001_init.sql")
    .read_text("utf-8")
)
_V2_SQL = (
    files("mlops_agents.storage")
    .joinpath("migrations", "project", "0002_stage2.sql")
    .read_text("utf-8")
)
_TS = "2026-10-25T00:00:00Z"


def _v2_database(tmp_path: Path) -> Path:
    """A project.db migrated to version 2 with sample data."""
    source = tmp_path / "v2_source"
    source.mkdir()
    (source / "0001_init.sql").write_text(_V1_SQL, encoding="utf-8")
    (source / "0002_stage2.sql").write_text(_V2_SQL, encoding="utf-8")
    db = tmp_path / "project.db"
    assert migrate(db, "project", tmp_path / "bk", source=source) == [1, 2]

    conn = connect_sync(db)
    try:
        conn.execute(
            "INSERT INTO sessions (id, agent, created_at) VALUES ('ses_1', 'orchestrator', ?)",
            (_TS,),
        )
        conn.execute(
            "INSERT INTO turns (id, session_id, trigger_message_id, status, created_at) "
            "VALUES ('trn_1', 'ses_1', 'msg_1', 'waiting', ?)",
            (_TS,),
        )
        conn.execute(
            "INSERT INTO turns (id, session_id, trigger_message_id, status, created_at) "
            "VALUES ('trn_2', 'ses_1', 'msg_2', 'waiting', ?)",
            (_TS,),
        )
        conn.execute(
            "INSERT INTO approvals (id, turn_id, session_id, message_id, agent, tool_call_id, "
            "tool, risk_class, preview, status, note, created_at, decided_at) "
            "VALUES ('apr_1', 'trn_1', 'ses_1', 'msg_1', 'orchestrator', 'call_1', "
            "'edit_file', 'write_code', '{\"kind\": \"diff\"}', 'pending', NULL, ?, NULL)",
            (_TS,),
        )
    finally:
        conn.close()
    return db


def test_migration_0003_preserves_data_adds_task_id_drops_unique_tool_call(tmp_path: Path) -> None:
    db = _v2_database(tmp_path)

    # Migrate to version 3
    result = migrate(db, "project", tmp_path / "bk")
    assert 3 in result

    conn = connect_sync(db)
    try:
        # Check existing row preserved
        row = conn.execute("SELECT * FROM approvals WHERE id = 'apr_1'").fetchone()
        assert row is not None

        # Verify task_id column exists
        columns = [str(r[1]) for r in conn.execute("PRAGMA table_info(approvals)")]
        assert "task_id" in columns

        # Same tool_call_id in a second approval is now permitted (NO UNIQUE constraint on tool_call_id)
        conn.execute(
            "INSERT INTO approvals (id, turn_id, session_id, message_id, agent, tool_call_id, "
            "tool, risk_class, preview, status, note, created_at, decided_at, task_id) "
            "VALUES ('apr_2', 'trn_2', 'ses_1', 'msg_2', 'orchestrator', 'call_1', "
            "'edit_file', 'write_code', '{\"kind\": \"diff\"}', 'pending', NULL, ?, NULL, 'tsk_1')",
            (_TS,),
        )

        rows = conn.execute(
            "SELECT id, tool_call_id, task_id FROM approvals WHERE tool_call_id = 'call_1' ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0] == ("apr_1", "call_1", None)
        assert rows[1] == ("apr_2", "call_1", "tsk_1")

        # Check indexes on turn_id and status
        indexes = {str(r[1]) for r in conn.execute("PRAGMA index_list(approvals)")}
        assert "approvals_status" in indexes
        assert "approvals_turn_id" in indexes

        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
