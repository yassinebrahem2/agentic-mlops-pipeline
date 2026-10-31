"""The stage 3 migrations: project.db 0004 and app.db 0002."""

import sqlite3
from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path

import pytest

from mlops_agents.storage.migrate import DbKind, migrate
from mlops_agents.storage.sqlite import connect_sync

_TS = "2026-10-28T00:00:00Z"


def _sql(kind: DbKind, name: str) -> str:
    return files("mlops_agents.storage").joinpath("migrations", kind, name).read_text("utf-8")


def _migrated_to(tmp_path: Path, kind: DbKind, names: list[str]) -> Path:
    """A database migrated with only the named migrations."""
    source = tmp_path / f"{kind}_source"
    source.mkdir()
    for name in names:
        (source / name).write_text(_sql(kind, name), encoding="utf-8")
    db = tmp_path / f"{kind}.db"
    assert migrate(db, kind, tmp_path / "bk", source=source) == list(range(1, len(names) + 1))
    return db


@pytest.fixture
def project_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "project.db"
    assert migrate(db, "project", tmp_path / "bk") == [1, 2, 3, 4, 5, 6, 7, 8]
    conn = connect_sync(db)
    _seed_turn(conn)
    yield conn
    conn.close()


def _seed_turn(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO sessions (id, agent, created_at) VALUES ('ses_1', 'onboarding', ?)", (_TS,)
    )
    conn.execute(
        "INSERT INTO turns (id, session_id, trigger_message_id, status, created_at) "
        "VALUES ('trn_1', 'ses_1', 'msg_1', 'waiting', ?)",
        (_TS,),
    )


def _turn_columns(conn: sqlite3.Connection) -> list[str]:
    return [str(r[1]) for r in conn.execute("PRAGMA table_info(turns)")]


def _count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608 -- test table names
    return int(row[0])


# project.db: migration from version 4 (0005 drops turns.error_kind, which nothing writes)


def test_version_4_database_drops_the_turn_error_kind_keeping_its_rows(tmp_path: Path) -> None:
    db = _migrated_to(
        tmp_path,
        "project",
        [
            "0001_init.sql",
            "0002_stage2.sql",
            "0003_approvals_tool_call.sql",
            "0004_stage3.sql",
        ],
    )
    conn = connect_sync(db)
    try:
        _seed_turn(conn)
        conn.execute("UPDATE turns SET status = 'failed', error = 'boom', error_kind = NULL")
        conn.execute(
            "INSERT INTO turns (id, session_id, trigger_message_id, status, created_at, "
            "error_kind) VALUES ('trn_2', 'ses_1', 'msg_2', 'failed', ?, 'keys_exhausted')",
            (_TS,),
        )
    finally:
        conn.close()

    assert migrate(db, "project", tmp_path / "bk") == [5, 6, 7, 8]

    conn = connect_sync(db)
    try:
        assert "error_kind" not in _turn_columns(conn)
        rows = conn.execute("SELECT id, status, error FROM turns ORDER BY id").fetchall()
        assert rows == [("trn_1", "failed", "boom"), ("trn_2", "failed", None)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_fresh_project_database_has_no_turn_error_kind(project_db: sqlite3.Connection) -> None:
    assert "error_kind" not in _turn_columns(project_db)


# project.db: migration from version 3


def test_version_3_database_migrates_with_its_approvals(tmp_path: Path) -> None:
    db = _migrated_to(
        tmp_path,
        "project",
        ["0001_init.sql", "0002_stage2.sql", "0003_approvals_tool_call.sql"],
    )
    conn = connect_sync(db)
    try:
        _seed_turn(conn)
        conn.execute(
            "INSERT INTO approvals (id, turn_id, session_id, message_id, agent, tool_call_id, "
            "tool, risk_class, preview, status, note, created_at, decided_at, task_id) "
            "VALUES ('apr_1', 'trn_1', 'ses_1', 'msg_1', 'coder', 'call_1', 'Edit', "
            "'write_code', '{}', 'approved', 'fine', ?, ?, 'tsk_1')",
            (_TS, _TS),
        )
    finally:
        conn.close()

    assert migrate(db, "project", tmp_path / "bk") == [4, 5, 6, 7, 8]
    backups = list((tmp_path / "bk").glob("*.db"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        # Taken before 0004 ran: the version 3 schema, with the approval row.
        assert "kind" not in [str(r[1]) for r in backup.execute("PRAGMA table_info(approvals)")]
        assert "error_kind" not in [str(r[1]) for r in backup.execute("PRAGMA table_info(turns)")]
        assert backup.execute("SELECT COUNT(*) FROM approvals").fetchone() == (1,)
    finally:
        backup.close()

    conn = connect_sync(db)
    try:
        row = conn.execute(
            "SELECT id, tool_call_id, tool, risk_class, status, note, task_id, kind, step, "
            "proposal_kind FROM approvals"
        ).fetchall()
        assert row == [
            (
                "apr_1",
                "call_1",
                "Edit",
                "write_code",
                "approved",
                "fine",
                "tsk_1",
                "tool",
                None,
                None,
            )
        ]
        indexes = {str(r[1]) for r in conn.execute("PRAGMA index_list(approvals)")}
        assert {"approvals_status", "approvals_turn_id", "approvals_stall_step"} <= indexes
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert "error_kind" not in _turn_columns(conn)
    finally:
        conn.close()


def test_fresh_project_database_gets_the_stage_3_tables(project_db: sqlite3.Connection) -> None:
    tables = {
        str(r[0]) for r in project_db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "connections",
        "tracking_sources",
        "tracked_models",
        "model_versions",
        "tracked_runs",
        "run_metrics",
        "deployments",
        "ci_runs",
        "evaluations",
        "notes",
        "notes_fts",
        "note_embeddings",
        "proposals",
        "handoff_versions",
        "kickoff",
    } <= tables


# project.db: CHECK constraints

_GOOD_TOOL_APPROVAL = {
    "kind": "'tool'",
    "tool_call_id": "'call_1'",
    "tool": "'Edit'",
    "risk_class": "'write_code'",
    "step": "NULL",
    "proposal_kind": "NULL",
}
_GOOD_STALL_APPROVAL = {
    "kind": "'stall'",
    "tool_call_id": "NULL",
    "tool": "NULL",
    "risk_class": "NULL",
    "step": "3",
    "proposal_kind": "NULL",
}


def _insert_approval(
    conn: sqlite3.Connection, approval_id: str, values: dict[str, str], status: str = "pending"
) -> None:
    columns = ", ".join(values)
    literals = ", ".join(values.values())
    conn.execute(
        "INSERT INTO approvals (id, turn_id, session_id, message_id, agent, preview, status, "  # noqa: S608 -- literals are test constants
        f"created_at, {columns}) VALUES (?, 'trn_1', 'ses_1', 'msg_1', 'coder', '{{}}', ?, ?, "
        f"{literals})",
        (approval_id, status, _TS),
    )


@pytest.mark.parametrize("values", [_GOOD_TOOL_APPROVAL, _GOOD_STALL_APPROVAL])
def test_approvals_check_accepts_both_kinds(
    project_db: sqlite3.Connection, values: dict[str, str]
) -> None:
    _insert_approval(project_db, "apr_1", values)
    _insert_approval(project_db, "apr_2", {**values, "proposal_kind": "NULL"}, status="approved")


@pytest.mark.parametrize(
    "values",
    [
        {**_GOOD_TOOL_APPROVAL, "tool_call_id": "NULL"},
        {**_GOOD_TOOL_APPROVAL, "tool": "NULL"},
        {**_GOOD_TOOL_APPROVAL, "risk_class": "NULL"},
        {**_GOOD_TOOL_APPROVAL, "step": "1"},
        {**_GOOD_STALL_APPROVAL, "tool_call_id": "'call_1'"},
        {**_GOOD_STALL_APPROVAL, "tool": "'Edit'"},
        {**_GOOD_STALL_APPROVAL, "risk_class": "'run'"},
        {**_GOOD_STALL_APPROVAL, "step": "NULL"},
        {**_GOOD_STALL_APPROVAL, "proposal_kind": "'handoff'"},
        {**_GOOD_TOOL_APPROVAL, "kind": "'other'"},
    ],
)
def test_approvals_check_rejects_mixed_kinds(
    project_db: sqlite3.Connection, values: dict[str, str]
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert_approval(project_db, "apr_1", values)


def test_approval_id_is_required(project_db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        project_db.execute(
            "INSERT INTO approvals (id, turn_id, session_id, message_id, agent, tool_call_id, "
            "tool, risk_class, preview, status, created_at) VALUES (NULL, 'trn_1', 'ses_1', "
            "'msg_1', 'coder', 'call_1', 'Edit', 'write_code', '{}', 'pending', ?)",
            (_TS,),
        )


def test_tool_approval_may_carry_a_proposal_kind(project_db: sqlite3.Connection) -> None:
    _insert_approval(project_db, "apr_1", {**_GOOD_TOOL_APPROVAL, "proposal_kind": "'handoff'"})


def test_one_pending_stall_per_turn_and_step(project_db: sqlite3.Connection) -> None:
    _insert_approval(project_db, "apr_1", _GOOD_STALL_APPROVAL)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_approval(project_db, "apr_2", _GOOD_STALL_APPROVAL)
    project_db.execute("UPDATE approvals SET status = 'approved' WHERE id = 'apr_1'")
    _insert_approval(project_db, "apr_2", _GOOD_STALL_APPROVAL)
    _insert_approval(project_db, "apr_3", {**_GOOD_STALL_APPROVAL, "step": "4"})


def _insert_source(conn: sqlite3.Connection, source_id: str = "trk_1") -> None:
    conn.execute(
        "INSERT INTO tracking_sources (id, type, settings, auth, created_at) "
        "VALUES (?, 'mlflow', '{}', 'none', ?)",
        (source_id, _TS),
    )


def _insert_note(conn: sqlite3.Connection, note_id: str, title: str, text: str) -> int:
    cursor = conn.execute(
        "INSERT INTO notes (id, type, title, text, source, author, created_at, updated_at) "
        "VALUES (?, 'fact', ?, ?, NULL, 'onboarding', ?, ?)",
        (note_id, title, text, _TS, _TS),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


# Each statement is valid except for one value that a CHECK refuses.
_BAD_VALUES = [
    "INSERT INTO connections (id, name, kind, settings_json, status, created, updated) "
    "VALUES ('con_1', 'hotel', 'local_folder', '{}', 'broken', 't', 't')",
    "INSERT INTO tracking_sources (id, type, settings, auth, created_at) "
    "VALUES ('trk_2', 'wandb', '{}', 'none', 't')",
    "INSERT INTO tracking_sources (id, type, settings, auth, created_at) "
    "VALUES ('trk_2', 'mlflow', '{}', 'token', 't')",
    "INSERT INTO model_versions (source_id, model, version, aliases, created_at, evaluation) "
    "VALUES ('trk_1', 'm', '1', '[]', 't', 'unknown')",
    "INSERT INTO tracked_runs (source_id, key, experiment, status, params, tags, datasets) "
    "VALUES ('trk_1', 'r1', 'e', 'paused', '{}', '{}', '[]')",
    "INSERT INTO ci_runs (source_id, key, workflow, status, started_at, commit_sha, url) "
    "VALUES ('trk_1', 'c1', 'ci', 'queued', 't', 'abc', 'u')",
    "INSERT INTO notes (id, type, title, text, author, created_at, updated_at) "
    "VALUES ('kno_9', 'rumour', 't', 'x', 'a', 't', 't')",
    "INSERT INTO proposals (id, kind, key, payload, status, agent, turn_id, tool_call_id, "
    "created_at) VALUES ('prp_1', 'handoff', 'k', '{}', 'maybe', 'a', 'trn_1', 'call_1', 't')",
    "INSERT INTO kickoff (handoff_version, agent, status) VALUES (1, 'coder', 'sent')",
]


@pytest.mark.parametrize("statement", _BAD_VALUES)
def test_checks_reject_bad_values(project_db: sqlite3.Connection, statement: str) -> None:
    _insert_source(project_db)
    _insert_note(project_db, "kno_1", "t", "x")
    project_db.execute(
        "INSERT INTO handoff_versions (proposal_id, approved_at, payload) VALUES ('prp_0', 't', '{}')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        project_db.execute(statement)


def test_model_version_evaluation_defaults_to_absent(project_db: sqlite3.Connection) -> None:
    _insert_source(project_db)
    project_db.execute(
        "INSERT INTO model_versions (source_id, model, version, aliases, created_at) "
        "VALUES ('trk_1', 'm', '1', '[]', 't')"
    )
    assert project_db.execute("SELECT evaluation FROM model_versions").fetchone() == ("absent",)


def test_draft_proposal_is_unique_by_turn_and_tool_call(project_db: sqlite3.Connection) -> None:
    insert = (
        "INSERT INTO proposals (id, kind, key, payload, status, agent, approval_id, turn_id, "
        "tool_call_id, created_at) VALUES (?, 'handoff', 'k', '{}', 'pending', 'a', ?, 'trn_1', ?, 't')"
    )
    project_db.execute(insert, ("prp_1", None, "call_1"))
    project_db.execute(insert, ("prp_2", None, "call_2"))
    with pytest.raises(sqlite3.IntegrityError):
        project_db.execute(insert, ("prp_3", None, "call_1"))
    project_db.execute("UPDATE proposals SET approval_id = 'apr_1' WHERE id = 'prp_1'")
    with pytest.raises(sqlite3.IntegrityError):
        project_db.execute("UPDATE proposals SET approval_id = 'apr_1' WHERE id = 'prp_2'")


def test_handoff_version_is_unique_per_proposal(project_db: sqlite3.Connection) -> None:
    insert = (
        "INSERT INTO handoff_versions (proposal_id, approved_at, payload) VALUES (?, 't', '{}')"
    )
    project_db.execute(insert, ("prp_1",))
    with pytest.raises(sqlite3.IntegrityError):
        project_db.execute(insert, ("prp_1",))


# project.db: cascades


_RECORD_ROWS = [
    "INSERT INTO tracked_models (source_id, name) VALUES ('trk_1', 'm')",
    "INSERT INTO model_versions (source_id, model, version, aliases, created_at) "
    "VALUES ('trk_1', 'm', '1', '[]', 't')",
    "INSERT INTO tracked_runs (source_id, key, experiment, status, params, tags, datasets) "
    "VALUES ('trk_1', 'r1', 'e', 'succeeded', '{}', '{}', '[]')",
    "INSERT INTO run_metrics (source_id, run_key, metric, step, time, value) "
    "VALUES ('trk_1', 'r1', 'auc', 0, 't', 0.9)",
    "INSERT INTO deployments (source_id, model, alias, seen_at, from_version, to_version) "
    "VALUES ('trk_1', 'm', 'production', 't', NULL, '1')",
    "INSERT INTO ci_runs (source_id, key, workflow, status, started_at, commit_sha, url) "
    "VALUES ('trk_1', 'c1', 'ci', 'failed', 't', 'abc', 'u')",
    "INSERT INTO evaluations (source_id, model, version, run_key, schema_version, payload, "
    "fetched_at) VALUES ('trk_1', 'm', '1', 'r1', 1, '{}', 't')",
]
_RECORD_TABLES = [
    "tracked_models",
    "model_versions",
    "tracked_runs",
    "run_metrics",
    "deployments",
    "ci_runs",
    "evaluations",
]


def test_record_rows_are_unique_by_source_and_key(project_db: sqlite3.Connection) -> None:
    _insert_source(project_db)
    for statement in _RECORD_ROWS:
        project_db.execute(statement)
        with pytest.raises(sqlite3.IntegrityError):
            project_db.execute(statement)


def test_removing_a_tracking_source_removes_its_records(project_db: sqlite3.Connection) -> None:
    _insert_source(project_db, "trk_1")
    _insert_source(project_db, "trk_2")
    for statement in _RECORD_ROWS:
        project_db.execute(statement)
        project_db.execute(statement.replace("'trk_1'", "'trk_2'"))
    project_db.execute("DELETE FROM tracking_sources WHERE id = 'trk_1'")
    for table in _RECORD_TABLES:
        assert _count(project_db, table) == 1, table
    assert project_db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_removing_a_note_removes_its_embedding(project_db: sqlite3.Connection) -> None:
    _insert_note(project_db, "kno_1", "a", "x")
    _insert_note(project_db, "kno_2", "b", "y")
    for note_id in ("kno_1", "kno_2"):
        project_db.execute(
            "INSERT INTO note_embeddings (note_id, embedder, dimensions, embedded_at) "
            "VALUES (?, 'p/e', 3, 't')",
            (note_id,),
        )
    project_db.execute("DELETE FROM notes WHERE id = 'kno_1'")
    assert project_db.execute("SELECT note_id FROM note_embeddings").fetchall() == [("kno_2",)]


def test_removing_a_handoff_version_removes_its_kickoff(project_db: sqlite3.Connection) -> None:
    for proposal in ("prp_1", "prp_2"):
        cursor = project_db.execute(
            "INSERT INTO handoff_versions (proposal_id, approved_at, payload) VALUES (?, 't', '{}')",
            (proposal,),
        )
        project_db.execute(
            "INSERT INTO kickoff (handoff_version, agent, status) VALUES (?, 'coder', 'pending')",
            (cursor.lastrowid,),
        )
    project_db.execute("DELETE FROM handoff_versions WHERE proposal_id = 'prp_1'")
    assert _count(project_db, "kickoff") == 1


# project.db: full-text search


def _matches(conn: sqlite3.Connection, query: str) -> list[int]:
    rows = conn.execute(
        "SELECT rowid FROM notes_fts WHERE notes_fts MATCH ? ORDER BY rowid", (query,)
    )
    return [int(r[0]) for r in rows]


def test_fts_follows_note_changes(project_db: sqlite3.Connection) -> None:
    first = _insert_note(project_db, "kno_1", "Churn model", "Retrained weekly")
    second = _insert_note(project_db, "kno_2", "Fraud model", "Retrained daily")
    assert _matches(project_db, "retrained") == [first, second]
    project_db.execute("UPDATE notes SET text = 'Retrained hourly' WHERE id = 'kno_1'")
    assert _matches(project_db, "weekly") == []
    assert _matches(project_db, "hourly") == [first]
    project_db.execute("DELETE FROM notes WHERE id = 'kno_2'")
    assert _matches(project_db, "fraud") == []
    assert _matches(project_db, "retrained") == [first]


def test_fts_still_matches_after_notes_is_rebuilt(project_db: sqlite3.Connection) -> None:
    _insert_note(project_db, "kno_1", "Drop", "removed before the rebuild")
    seq = _insert_note(project_db, "kno_2", "Churn model", "Retrained weekly")
    project_db.execute("DELETE FROM notes WHERE id = 'kno_1'")
    triggers = [
        str(r[0])
        for r in project_db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'notes'"
        )
    ]
    notes_sql = project_db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'notes'"
    ).fetchone()[0]
    project_db.executescript(
        "BEGIN;\n"  # noqa: S608 -- the SQL is the schema's own, read back from sqlite_master
        + str(notes_sql).replace("CREATE TABLE notes", "CREATE TABLE notes_new", 1)
        + ";\nINSERT INTO notes_new SELECT * FROM notes;\n"
        "DROP TABLE notes;\n"
        "ALTER TABLE notes_new RENAME TO notes;\n"
        + ";\n".join(triggers)
        + ";\nINSERT INTO notes_fts (notes_fts) VALUES ('rebuild');\n"
        "COMMIT;"
    )
    assert _matches(project_db, "weekly") == [seq]
    assert project_db.execute(
        "SELECT notes.id FROM notes_fts JOIN notes ON notes.seq = notes_fts.rowid "
        "WHERE notes_fts MATCH 'churn'"
    ).fetchall() == [("kno_2",)]
    assert _insert_note(project_db, "kno_3", "New", "after") > seq


# app.db


def test_version_1_app_database_migrates_with_its_rows(tmp_path: Path) -> None:
    db = _migrated_to(tmp_path, "app", ["0001_init.sql"])
    conn = connect_sync(db)
    try:
        conn.execute(
            "INSERT INTO providers (id, name, kind, base_url, created_at) "
            "VALUES ('prv_1', 'groq', 'hosted', 'https://api.groq.com', ?)",
            (_TS,),
        )
        conn.execute(
            "INSERT INTO models (address, provider_id, wire_model_id, context_window) "
            "VALUES ('groq/llama', 'prv_1', 'llama', 8192)"
        )
    finally:
        conn.close()

    assert migrate(db, "app", tmp_path / "bk") == [2, 3]

    conn = connect_sync(db)
    try:
        assert conn.execute("SELECT name, cooldown_minutes FROM providers").fetchall() == [
            ("groq", 15)
        ]
        assert conn.execute(
            "SELECT address, context_window, model_type, dimensions FROM models"
        ).fetchall() == [("groq/llama", 8192, "chat", None)]
        assert _count(conn, "secret_refs") == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE providers SET cooldown_minutes = 0",
        "UPDATE models SET model_type = 'reranker'",
        "UPDATE models SET dimensions = 0",
    ],
)
def test_app_checks_reject_bad_values(tmp_path: Path, statement: str) -> None:
    db = tmp_path / "app.db"
    assert migrate(db, "app", tmp_path / "bk") == [1, 2, 3]
    conn = connect_sync(db)
    try:
        conn.execute(
            "INSERT INTO providers (id, name, kind, base_url, created_at) "
            "VALUES ('prv_1', 'groq', 'hosted', 'https://api.groq.com', ?)",
            (_TS,),
        )
        conn.execute(
            "INSERT INTO models (address, provider_id, wire_model_id) "
            "VALUES ('groq/e', 'prv_1', 'e')"
        )
        conn.execute("UPDATE models SET model_type = 'embedder', dimensions = 768")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(statement)
    finally:
        conn.close()
