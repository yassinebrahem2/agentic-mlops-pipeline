"""Tests for stage 4-5 schema migrations, tables, constraints, and monitoring settings accessor."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path

import pytest
from pydantic import ValidationError

from mlops_agents.monitoring.settings import (
    MonitoringSettings,
    get_monitoring_settings,
    set_monitoring_settings,
)
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.migrate import DbKind, migrate
from mlops_agents.storage.sqlite import connect_sync

_TS = "2026-10-29T12:00:00Z"


def _sql(kind: DbKind, name: str) -> str:
    return files("mlops_agents.storage").joinpath("migrations", kind, name).read_text("utf-8")


def _migrated_to(tmp_path: Path, kind: DbKind, names: list[str]) -> Path:
    source = tmp_path / f"{kind}_source"
    source.mkdir(parents=True, exist_ok=True)
    for name in names:
        (source / name).write_text(_sql(kind, name), encoding="utf-8")
    db = tmp_path / f"{kind}.db"
    assert migrate(db, kind, tmp_path / "bk", source=source) == list(range(1, len(names) + 1))
    return db


@pytest.fixture
def project_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "project.db"
    assert migrate(db, "project", tmp_path / "bk") == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    conn = connect_sync(db)
    yield conn
    conn.close()


@pytest.fixture
def app_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "app.db"
    assert migrate(db, "app", tmp_path / "bk") == [1, 2, 3, 4]
    conn = connect_sync(db)
    yield conn
    conn.close()


def test_fresh_databases_migrate_to_latest(tmp_path: Path) -> None:
    app_db_path = tmp_path / "app.db"
    project_db_path = tmp_path / "project.db"
    assert migrate(app_db_path, "app", tmp_path / "bk") == [1, 2, 3, 4]
    assert migrate(project_db_path, "project", tmp_path / "bk") == [
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
    ]


def test_v6_project_db_migrates_to_v7_preserving_data(tmp_path: Path) -> None:
    db = _migrated_to(
        tmp_path,
        "project",
        [
            "0001_init.sql",
            "0002_stage2.sql",
            "0003_approvals_tool_call.sql",
            "0004_stage3.sql",
            "0005_drop_turn_error_kind.sql",
            "0006_approval_settling.sql",
        ],
    )
    conn = connect_sync(db)
    try:
        conn.execute(
            "INSERT INTO sessions (id, agent, created_at) VALUES ('ses_1', 'onboarding', ?)",
            (_TS,),
        )
        conn.execute(
            "INSERT INTO turns (id, session_id, trigger_message_id, status, created_at) "
            "VALUES ('trn_1', 'ses_1', 'msg_1', 'waiting', ?)",
            (_TS,),
        )
        conn.execute(
            "INSERT INTO events (id, ts, source, type, subject_id, payload) "
            "VALUES ('evt_1', ?, 'system', 'session.created', 'ses_1', '{}')",
            (_TS,),
        )
    finally:
        conn.close()

    assert migrate(db, "project", tmp_path / "bk") == [7, 8, 9, 10]

    conn = connect_sync(db)
    try:
        assert conn.execute("SELECT id FROM sessions").fetchall() == [("ses_1",)]
        assert conn.execute("SELECT id FROM turns").fetchall() == [("trn_1",)]
        assert conn.execute("SELECT id FROM events").fetchall() == [("evt_1",)]
        # Check that events_subject_seq index exists
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'events'"
            )
        }
        assert "events_subject_seq" in indexes
    finally:
        conn.close()


def test_v2_app_db_migrates_to_v3_preserving_data(tmp_path: Path) -> None:
    db = _migrated_to(
        tmp_path,
        "app",
        [
            "0001_init.sql",
            "0002_stage3.sql",
        ],
    )
    conn = connect_sync(db)
    try:
        conn.execute(
            "INSERT INTO providers (id, name, kind, base_url, cooldown_minutes, created_at) "
            "VALUES ('prv_1', 'groq', 'hosted', 'https://api.groq.com', 15, ?)",
            (_TS,),
        )
        conn.execute(
            "INSERT INTO models (address, provider_id, wire_model_id, context_window, "
            "tool_calling, streaming, availability, model_type) "
            "VALUES ('groq/llama', 'prv_1', 'llama', 8192, 1, 1, 'verified', 'chat')",
        )
    finally:
        conn.close()

    assert migrate(db, "app", tmp_path / "bk") == [3, 4]

    conn = connect_sync(db)
    try:
        row = conn.execute(
            "SELECT address, price_prompt_per_mtok, price_completion_per_mtok, price_source "
            "FROM models WHERE address = 'groq/llama'"
        ).fetchone()
        assert row == ("groq/llama", None, None, None)
    finally:
        conn.close()


def test_app_db_price_check_constraint(app_db: sqlite3.Connection) -> None:
    app_db.execute(
        "INSERT INTO providers (id, name, kind, base_url, cooldown_minutes, created_at) "
        "VALUES ('prv_1', 'groq', 'hosted', 'https://api.groq.com', 15, ?)",
        (_TS,),
    )
    # Valid: all three NULL
    app_db.execute(
        "INSERT INTO models (address, provider_id, wire_model_id, tool_calling, streaming, "
        "availability, model_type, price_prompt_per_mtok, price_completion_per_mtok, price_source) "
        "VALUES ('groq/m1', 'prv_1', 'm1', 1, 1, 'verified', 'chat', NULL, NULL, NULL)"
    )
    # Valid: all three set
    app_db.execute(
        "INSERT INTO models (address, provider_id, wire_model_id, tool_calling, streaming, "
        "availability, model_type, price_prompt_per_mtok, price_completion_per_mtok, price_source) "
        "VALUES ('groq/m2', 'prv_1', 'm2', 1, 1, 'verified', 'chat', 0.1, 0.2, 'provider')"
    )
    app_db.execute(
        "INSERT INTO models (address, provider_id, wire_model_id, tool_calling, streaming, "
        "availability, model_type, price_prompt_per_mtok, price_completion_per_mtok, price_source) "
        "VALUES ('groq/m3', 'prv_1', 'm3', 1, 1, 'verified', 'chat', 0.5, 1.5, 'user')"
    )

    # Invalid: partial prices or invalid source
    invalid_combos = [
        (0.1, None, "user"),
        (None, 0.2, "user"),
        (0.1, 0.2, None),
        (None, None, "user"),
        (0.1, None, None),
        (None, 0.2, None),
        (0.1, 0.2, "other"),
    ]
    for idx, (p1, p2, src) in enumerate(invalid_combos):
        with pytest.raises(sqlite3.IntegrityError):
            app_db.execute(
                "INSERT INTO models (address, provider_id, wire_model_id, tool_calling, streaming, "
                "availability, model_type, price_prompt_per_mtok, price_completion_per_mtok, price_source) "
                "VALUES (?, 'prv_1', 'bad', 1, 1, 'verified', 'chat', ?, ?, ?)",
                (f"groq/bad_{idx}", p1, p2, src),
            )


def test_project_unique_constraints(project_db: sqlite3.Connection) -> None:
    # 1. results: unique (source_id, source_key)
    project_db.execute(
        "INSERT INTO results (id, check_id, origin, ran_at, fired, category, subject, model, "
        "detail, source_ref, not_enough_data, source_id, source_key, seen_at) "
        "VALUES ('res_1', 'chk_1', 'agent', ?, 0, 'data_quality', 'data', NULL, '{}', 'ref1', 0, "
        "'src_1', 'key_1', ?)",
        (_TS, _TS),
    )
    with pytest.raises(sqlite3.IntegrityError):
        project_db.execute(
            "INSERT INTO results (id, check_id, origin, ran_at, fired, category, subject, model, "
            "detail, source_ref, not_enough_data, source_id, source_key, seen_at) "
            "VALUES ('res_2', 'chk_1', 'agent', ?, 0, 'data_quality', 'data', NULL, '{}', 'ref1', 0, "
            "'src_1', 'key_1', ?)",
            (_TS, _TS),
        )

    # 2. expectations: unique (kind, ref, coalesce(model, ''))
    project_db.execute(
        "INSERT INTO expectations (kind, ref, model, spec, active, last_seen, last_signalled_first, last_signalled_second, registered_at) "
        "VALUES ('feed', 'feed_1', NULL, '{}', 1, NULL, NULL, NULL, '2026-10-29T12:00:00.000Z')"
    )
    # Duplicate with model=NULL must be refused
    with pytest.raises(sqlite3.IntegrityError):
        project_db.execute(
            "INSERT INTO expectations (kind, ref, model, spec, active, last_seen, last_signalled_first, last_signalled_second, registered_at) "
            "VALUES ('feed', 'feed_1', NULL, '{}', 1, NULL, NULL, NULL, '2026-10-29T12:00:00.000Z')"
        )
    # With a specific model, allowed
    project_db.execute(
        "INSERT INTO expectations (kind, ref, model, spec, active, last_seen, last_signalled_first, last_signalled_second, registered_at) "
        "VALUES ('feed', 'feed_1', 'm1', '{}', 1, NULL, NULL, NULL, '2026-10-29T12:00:00.000Z')"
    )
    # Duplicate with same model refused
    with pytest.raises(sqlite3.IntegrityError):
        project_db.execute(
            "INSERT INTO expectations (kind, ref, model, spec, active, last_seen, last_signalled_first, last_signalled_second, registered_at) "
            "VALUES ('feed', 'feed_1', 'm1', '{}', 1, NULL, NULL, NULL, '2026-10-29T12:00:00.000Z')"
        )

    # 3. scheduler_jobs: unique (kind, due, subject)
    project_db.execute(
        "INSERT INTO scheduler_jobs (kind, due, subject, started, finished, error) "
        "VALUES ('sync_tracking', ?, '', NULL, NULL, NULL)",
        (_TS,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        project_db.execute(
            "INSERT INTO scheduler_jobs (kind, due, subject, started, finished, error) "
            "VALUES ('sync_tracking', ?, '', NULL, NULL, NULL)",
            (_TS,),
        )

    # 4. signals: unique result_id where result_id IS NOT NULL
    project_db.execute(
        "INSERT INTO signals (id, incident_id, kind, category, subject, model, ran_at, seen_at, "
        "cause_key, detail, result_id, source_ref, expectation, cleared) "
        "VALUES ('sig_1', 'inc_1', 'fired_result', 'data_quality', 'sub', NULL, ?, ?, 'key', '{}', "
        "'res_1', NULL, NULL, 0)",
        (_TS, _TS),
    )
    with pytest.raises(sqlite3.IntegrityError):
        project_db.execute(
            "INSERT INTO signals (id, incident_id, kind, category, subject, model, ran_at, seen_at, "
            "cause_key, detail, result_id, source_ref, expectation, cleared) "
            "VALUES ('sig_2', 'inc_1', 'fired_result', 'data_quality', 'sub', NULL, ?, ?, 'key', '{}', "
            "'res_1', NULL, NULL, 0)",
            (_TS, _TS),
        )
    # Signals with NULL result_id can have duplicates
    project_db.execute(
        "INSERT INTO signals (id, incident_id, kind, category, subject, model, ran_at, seen_at, "
        "cause_key, detail, result_id, source_ref, expectation, cleared) "
        "VALUES ('sig_3', 'inc_1', 'silence', 'operational', 'sub', NULL, ?, ?, 'key', '{}', "
        "NULL, NULL, NULL, 0)",
        (_TS, _TS),
    )
    project_db.execute(
        "INSERT INTO signals (id, incident_id, kind, category, subject, model, ran_at, seen_at, "
        "cause_key, detail, result_id, source_ref, expectation, cleared) "
        "VALUES ('sig_4', 'inc_1', 'silence', 'operational', 'sub', NULL, ?, ?, 'key', '{}', "
        "NULL, NULL, NULL, 0)",
        (_TS, _TS),
    )

    # 5. agent_wakes: unique (agent, label, subject, key)
    project_db.execute(
        "INSERT INTO agent_wakes (agent, label, subject, key, text, created, posted_at) "
        "VALUES ('diagnoser', 'incident', 'inc_1', 'k1', 'wake text', ?, NULL)",
        (_TS,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        project_db.execute(
            "INSERT INTO agent_wakes (agent, label, subject, key, text, created, posted_at) "
            "VALUES ('diagnoser', 'incident', 'inc_1', 'k1', 'wake text', ?, NULL)",
            (_TS,),
        )

    # 6. hooks: unique (coalesce(model, ''), name)
    project_db.execute(
        "INSERT INTO hooks (id, name, model, spec, status, source, created) "
        "VALUES ('hok_1', 'rollback', NULL, '{}', 'active', 'handoff', ?)",
        (_TS,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        project_db.execute(
            "INSERT INTO hooks (id, name, model, spec, status, source, created) "
            "VALUES ('hok_2', 'rollback', NULL, '{}', 'active', 'handoff', ?)",
            (_TS,),
        )
    project_db.execute(
        "INSERT INTO hooks (id, name, model, spec, status, source, created) "
        "VALUES ('hok_3', 'rollback', 'm1', '{}', 'active', 'handoff', ?)",
        (_TS,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        project_db.execute(
            "INSERT INTO hooks (id, name, model, spec, status, source, created) "
            "VALUES ('hok_4', 'rollback', 'm1', '{}', 'active', 'handoff', ?)",
            (_TS,),
        )

    # 7. baseline_links: unique (incident_id, team_check_id)
    project_db.execute(
        "INSERT INTO baseline_links (incident_id, team_check_id, first_fired_at, seen_at, lead_seconds) "
        "VALUES ('inc_1', 'chk_team_1', ?, ?, 10.0)",
        (_TS, _TS),
    )
    with pytest.raises(sqlite3.IntegrityError):
        project_db.execute(
            "INSERT INTO baseline_links (incident_id, team_check_id, first_fired_at, seen_at, lead_seconds) "
            "VALUES ('inc_1', 'chk_team_1', ?, ?, 15.0)",
            (_TS, _TS),
        )

    # 8. proposal_id on the five tables:
    # 8a. parsers: unique proposal_id
    project_db.execute(
        "INSERT INTO parsers (source_id, version, spec, proposal_id, approved, enabled, consecutive_failures) "
        "VALUES ('src_1', 1, '{}', 'prp_1', ?, 1, 0)",
        (_TS,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        project_db.execute(
            "INSERT INTO parsers (source_id, version, spec, proposal_id, approved, enabled, consecutive_failures) "
            "VALUES ('src_2', 1, '{}', 'prp_1', ?, 1, 0)",
            (_TS,),
        )

    # 8b. rules: unique proposal_id
    project_db.execute(
        "INSERT INTO rules (id, spec, enabled, proposal_id, approved) "
        "VALUES ('rul_1', '{}', 1, 'prp_2', ?)",
        (_TS,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        project_db.execute(
            "INSERT INTO rules (id, spec, enabled, proposal_id, approved) "
            "VALUES ('rul_2', '{}', 1, 'prp_2', ?)",
            (_TS,),
        )

    # 8c. maintenance_windows: unique proposal_id where proposal_id IS NOT NULL
    project_db.execute(
        "INSERT INTO maintenance_windows (id, start, end, scope, reason, proposal_id, created_by, created) "
        "VALUES ('win_1', ?, ?, '[]', 'maint', 'prp_3', 'user', ?)",
        (_TS, _TS, _TS),
    )
    with pytest.raises(sqlite3.IntegrityError):
        project_db.execute(
            "INSERT INTO maintenance_windows (id, start, end, scope, reason, proposal_id, created_by, created) "
            "VALUES ('win_2', ?, ?, '[]', 'maint', 'prp_3', 'user', ?)",
            (_TS, _TS, _TS),
        )
    # NULL proposal_id can repeat
    project_db.execute(
        "INSERT INTO maintenance_windows (id, start, end, scope, reason, proposal_id, created_by, created) "
        "VALUES ('win_3', ?, ?, '[]', 'maint', NULL, 'user', ?)",
        (_TS, _TS, _TS),
    )
    project_db.execute(
        "INSERT INTO maintenance_windows (id, start, end, scope, reason, proposal_id, created_by, created) "
        "VALUES ('win_4', ?, ?, '[]', 'maint', NULL, 'user', ?)",
        (_TS, _TS, _TS),
    )

    # 8d. training_policies: unique proposal_id
    project_db.execute(
        "INSERT INTO training_policies (id, payload, status, proposal_id, approved) "
        "VALUES ('pol_1', '{}', 'active', 'prp_4', ?)",
        (_TS,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        project_db.execute(
            "INSERT INTO training_policies (id, payload, status, proposal_id, approved) "
            "VALUES ('pol_2', '{}', 'active', 'prp_4', ?)",
            (_TS,),
        )

    # 8e. promotions: unique proposal_id where proposal_id IS NOT NULL
    project_db.execute(
        "INSERT INTO promotions (id, model, candidate_version, mode, decided_by, status, created, proposal_id) "
        "VALUES ('prm_1', 'm1', 'v1', 'person', 'person', 'proposed', ?, 'prp_5')",
        (_TS,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        project_db.execute(
            "INSERT INTO promotions (id, model, candidate_version, mode, decided_by, status, created, proposal_id) "
            "VALUES ('prm_2', 'm2', 'v2', 'person', 'person', 'proposed', ?, 'prp_5')",
            (_TS,),
        )
    # NULL proposal_id can repeat
    project_db.execute(
        "INSERT INTO promotions (id, model, candidate_version, mode, decided_by, status, created, proposal_id) "
        "VALUES ('prm_3', 'm1', 'v2', 'person', 'person', 'proposed', ?, NULL)",
        (_TS,),
    )
    project_db.execute(
        "INSERT INTO promotions (id, model, candidate_version, mode, decided_by, status, created, proposal_id) "
        "VALUES ('prm_4', 'm1', 'v3', 'person', 'person', 'proposed', ?, NULL)",
        (_TS,),
    )

    # 9. promotions: unique (model, candidate_version) where mode = 'team_gate'
    project_db.execute(
        "INSERT INTO promotions (id, model, candidate_version, mode, decided_by, status, created) "
        "VALUES ('prm_tg_1', 'm_gate', 'v1', 'team_gate', 'team_gate', 'proposed', ?)",
        (_TS,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        project_db.execute(
            "INSERT INTO promotions (id, model, candidate_version, mode, decided_by, status, created) "
            "VALUES ('prm_tg_2', 'm_gate', 'v1', 'team_gate', 'team_gate', 'proposed', ?)",
            (_TS,),
        )
    # In mode = 'person', duplicates of same (model, candidate_version) are allowed
    project_db.execute(
        "INSERT INTO promotions (id, model, candidate_version, mode, decided_by, status, created) "
        "VALUES ('prm_p_1', 'm_person', 'v1', 'person', 'person', 'proposed', ?)",
        (_TS,),
    )
    project_db.execute(
        "INSERT INTO promotions (id, model, candidate_version, mode, decided_by, status, created) "
        "VALUES ('prm_p_2', 'm_person', 'v1', 'person', 'person', 'proposed', ?)",
        (_TS,),
    )


@pytest.mark.asyncio
async def test_monitoring_settings_accessor(tmp_path: Path) -> None:
    paths = DataPaths(tmp_path)
    project_dir = paths.project_dir("prj_1")
    project_dir.mkdir(parents=True)
    db_path = project_dir / "project.db"
    migrate(db_path, "project", tmp_path / "bk")

    databases = Databases(paths)
    try:
        # Default settings when empty
        settings = await get_monitoring_settings(databases, "prj_1")
        assert settings == MonitoringSettings()

        # Update and read back
        updated = MonitoringSettings(
            collection_interval_s=120,
            grouping_window_minutes=45,
            timezone="UTC",
        )
        saved = await set_monitoring_settings(databases, "prj_1", updated)
        assert saved == updated

        read_back = await get_monitoring_settings(databases, "prj_1")
        assert read_back == updated

        # Validation refused before write: collection_interval_s < 10
        with pytest.raises((ValidationError, ValueError)):
            await set_monitoring_settings(
                databases,
                "prj_1",
                {"collection_interval_s": 5},  # type: ignore[arg-type]
            )

        # Database remains unchanged
        unchanged = await get_monitoring_settings(databases, "prj_1")
        assert unchanged == updated
    finally:
        await databases.dispose()
