"""Tests for migration 0011: deployments baseline column."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from mlops_agents.storage.migrate import migrate
from mlops_agents.storage.sqlite import connect_sync
from mlops_agents.tracking.contract import TrackingQuery
from mlops_agents.tracking.queries import query_deployments

_UP_TO_10 = [
    "0001_init.sql",
    "0002_stage2.sql",
    "0003_approvals_tool_call.sql",
    "0004_stage3.sql",
    "0005_drop_turn_error_kind.sql",
    "0006_approval_settling.sql",
    "0007_stage4_5.sql",
    "0008_expectations_registered_at.sql",
    "0009_result_delivery_attempts.sql",
    "0010_fix_requests_active_unique.sql",
]

_INSERT_SOURCE = (
    "INSERT INTO tracking_sources (id, type, settings, auth, created_at) VALUES (?, ?, ?, ?, ?)"
)

_INSERT_OLD = (
    "INSERT INTO deployments (source_id, model, alias, seen_at, from_version, to_version) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


def _version_10(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    for name in _UP_TO_10:
        sql = files("mlops_agents.storage").joinpath("migrations", "project", name)
        (source / name).write_text(sql.read_text("utf-8"), encoding="utf-8")
    db = tmp_path / "project.db"
    assert migrate(db, "project", tmp_path / "bk", source=source) == list(range(1, 11))
    return db


@pytest.mark.anyio
async def test_migration_0011_existing_rows_default_to_zero_and_query_as_false(
    tmp_path: Path,
) -> None:
    db = _version_10(tmp_path)
    conn = connect_sync(db)
    try:
        conn.execute(
            _INSERT_SOURCE,
            ("src_1", "mlflow", "{}", "none", "2026-10-30T09:00:00.000Z"),
        )
        conn.execute(
            _INSERT_OLD,
            ("src_1", "classifier", "production", "2026-10-30T10:00:00.000Z", None, "1"),
        )
    finally:
        conn.close()

    # Migrate to 0011
    applied = migrate(db, "project", tmp_path / "bk")
    assert 11 in applied

    # Verify directly via sqlite3
    conn = connect_sync(db)
    try:
        row = conn.execute(
            "SELECT source_id, model, alias, seen_at, from_version, to_version, baseline FROM deployments"
        ).fetchone()
        assert row is not None
        assert row[6] == 0
    finally:
        conn.close()

    # Verify via query_deployments
    engine = create_async_engine(f"sqlite+aiosqlite:///{db.as_posix()}")
    try:
        async with engine.connect() as async_conn:
            page = await query_deployments(async_conn, TrackingQuery(what="deployments"))
            assert len(page.rows) == 1
            assert page.rows[0].baseline is False
    finally:
        await engine.dispose()
