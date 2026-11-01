"""project.db 0009: results table gets delivery_attempts column defaulting to 0."""

from importlib.resources import files
from pathlib import Path

from mlops_agents.storage.migrate import migrate
from mlops_agents.storage.sqlite import connect_sync

_UP_TO_8 = [
    "0001_init.sql",
    "0002_stage2.sql",
    "0003_approvals_tool_call.sql",
    "0004_stage3.sql",
    "0005_drop_turn_error_kind.sql",
    "0006_approval_settling.sql",
    "0007_stage4_5.sql",
    "0008_expectations_registered_at.sql",
]


def _version_8(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    for name in _UP_TO_8:
        sql = files("mlops_agents.storage").joinpath("migrations", "project", name)
        (source / name).write_text(sql.read_text("utf-8"), encoding="utf-8")
    db = tmp_path / "project.db"
    assert migrate(db, "project", tmp_path / "bk", source=source) == list(range(1, 9))
    return db


def test_a_version_8_database_migrates_to_v9_and_defaults_delivery_attempts(
    tmp_path: Path,
) -> None:
    db = _version_8(tmp_path)
    conn = connect_sync(db)
    # Insert a result row at v8 schema
    conn.execute(
        """
        INSERT INTO results (
            id, check_id, origin, ran_at, fired, category, subject, detail,
            source_ref, not_enough_data, source_id, source_key, seen_at
        ) VALUES (
            'res_1', 'chk_1', 'agent', '2026-10-30T10:00:00Z', 0, 'data_quality',
            'subject_1', '{}', 'ref_1', 0, 'src_1', 'key_1', '2026-10-30T10:00:00Z'
        )
        """
    )
    conn.commit()
    conn.close()

    # Now apply migration 0009 using the packaged migrations
    assert migrate(db, "project", tmp_path / "bk") == [9, 10]

    conn = connect_sync(db)
    # Check column exists and existing row has delivery_attempts = 0
    row = conn.execute("SELECT id, delivery_attempts FROM results WHERE id = 'res_1'").fetchone()
    assert row is not None
    assert row[0] == "res_1"
    assert row[1] == 0

    # Insert a new row without specifying delivery_attempts
    conn.execute(
        """
        INSERT INTO results (
            id, check_id, origin, ran_at, fired, category, subject, detail,
            source_ref, not_enough_data, source_id, source_key, seen_at
        ) VALUES (
            'res_2', 'chk_2', 'team', '2026-10-30T11:00:00Z', 1, 'drift',
            'subject_2', '{}', 'ref_2', 0, 'src_2', 'key_2', '2026-10-30T11:00:00Z'
        )
        """
    )
    conn.commit()
    row2 = conn.execute("SELECT id, delivery_attempts FROM results WHERE id = 'res_2'").fetchone()
    assert row2 is not None
    assert row2[0] == "res_2"
    assert row2[1] == 0
    conn.close()
