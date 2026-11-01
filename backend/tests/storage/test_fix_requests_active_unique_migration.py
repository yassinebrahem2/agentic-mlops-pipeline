"""project.db 0010: unique partial index on fix_requests (incident_id) for active statuses."""

import sqlite3
from importlib.resources import files
from pathlib import Path

import pytest

from mlops_agents.storage.migrate import migrate
from mlops_agents.storage.sqlite import connect_sync

_UP_TO_9 = [
    "0001_init.sql",
    "0002_stage2.sql",
    "0003_approvals_tool_call.sql",
    "0004_stage3.sql",
    "0005_drop_turn_error_kind.sql",
    "0006_approval_settling.sql",
    "0007_stage4_5.sql",
    "0008_expectations_registered_at.sql",
    "0009_result_delivery_attempts.sql",
]


def _version_9(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    for name in _UP_TO_9:
        sql = files("mlops_agents.storage").joinpath("migrations", "project", name)
        (source / name).write_text(sql.read_text("utf-8"), encoding="utf-8")
    db = tmp_path / "project.db"
    assert migrate(db, "project", tmp_path / "bk", source=source) == list(range(1, 10))
    return db


def test_v9_migrates_to_v10_and_enforces_one_active_fix_per_incident(tmp_path: Path) -> None:
    db = _version_9(tmp_path)
    conn = connect_sync(db)

    # Insert an existing fix at v9 schema
    conn.execute(
        """
        INSERT INTO fix_requests (
            id, incident_id, model, requested_by, evidence, choice, why, why_not,
            status, outcome, created
        ) VALUES (
            'fix_1', 'inc_1', 'churn_model', 'diagnoser', 'evidence 1', NULL, NULL, '{}',
            'requested', 'pending', '2026-10-31T10:00:00Z'
        )
        """
    )
    conn.commit()
    conn.close()

    # Apply migration 0010
    assert migrate(db, "project", tmp_path / "bk") == [10]

    conn = connect_sync(db)

    # 1. Second active fix for same incident (requested) is rejected
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO fix_requests (
                id, incident_id, model, requested_by, evidence, choice, why, why_not,
                status, outcome, created
            ) VALUES (
                'fix_2', 'inc_1', 'churn_model', 'diagnoser', 'evidence 2', NULL, NULL, '{}',
                'requested', 'pending', '2026-10-31T10:05:00Z'
            )
            """
        )

    # 2. Second active fix with chosen or running status is also rejected
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO fix_requests (
                id, incident_id, model, requested_by, evidence, choice, why, why_not,
                status, outcome, created
            ) VALUES (
                'fix_3', 'inc_1', 'churn_model', 'diagnoser', 'evidence 3', 'retrain', 'why', '{}',
                'running', 'pending', '2026-10-31T10:10:00Z'
            )
            """
        )

    # 3. Active fix for a different incident is allowed
    conn.execute(
        """
        INSERT INTO fix_requests (
            id, incident_id, model, requested_by, evidence, choice, why, why_not,
            status, outcome, created
        ) VALUES (
            'fix_diff_inc', 'inc_2', 'churn_model', 'diagnoser', 'evidence diff', NULL, NULL, '{}',
            'requested', 'pending', '2026-10-31T10:15:00Z'
        )
        """
    )
    conn.commit()

    # 4. Inactive fixes (done, declined, escalated) for the same incident are allowed
    conn.execute(
        """
        INSERT INTO fix_requests (
            id, incident_id, model, requested_by, evidence, choice, why, why_not,
            status, outcome, created
        ) VALUES (
            'fix_done', 'inc_1', 'churn_model', 'diagnoser', 'evidence', 'retrain', 'why', '{}',
            'done', 'cleared', '2026-10-31T10:20:00Z'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO fix_requests (
            id, incident_id, model, requested_by, evidence, choice, why, why_not,
            status, outcome, created
        ) VALUES (
            'fix_declined', 'inc_1', 'churn_model', 'diagnoser', 'evidence', 'decline', 'why', '{}',
            'declined', 'pending', '2026-10-31T10:25:00Z'
        )
        """
    )
    conn.commit()

    rows = conn.execute("SELECT id FROM fix_requests WHERE incident_id = 'inc_1'").fetchall()
    assert len(rows) == 3  # fix_1 (requested), fix_done (done), fix_declined (declined)

    conn.close()
