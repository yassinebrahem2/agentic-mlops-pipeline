"""Tests for evaluation copy parsing and SqliteEvaluationCopies (Task 4 - issue #460)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.results.contract import source_id
from mlops_agents.monitoring.results.evaluation_copy import SqliteEvaluationCopies
from mlops_agents.monitoring.results.file_source import FileSourceReader
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import FileResults, HandoffPayload, Operations
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import evaluations, model_versions, projects
from mlops_agents.tracking.contract import EvaluationRecord, SourceType, TrackingQuery
from mlops_agents.tracking.service import TrackingService
from tests.conftest import MemoryKeyStore
from tests.monitoring.results.conftest import FakeConnections, FakeHandoffs
from tests.proposals.samples import handoff as sample_handoff_dict
from tests.tracking.conftest import (
    FakeHandoffs as TrackingFakeHandoffs,
)
from tests.tracking.conftest import (
    FakeTrackingAdapter,
    TrackingEnv,
)
from tests.tracking.conftest import TestClock as ClockForTest


@pytest.fixture
async def tracking_env(
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
) -> TrackingEnv:
    project_id = "prj_evaltest"
    engine = await databases.app()
    async with engine.begin() as conn:
        await conn.execute(
            insert(projects).values(
                id=project_id,
                name="Eval Test Project",
                source="github",
                github_full_name="org/demo",
                github_key="org/demo",
                default_branch="main",
                status="ok",
                created_at=datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC).isoformat(),
            )
        )
    await databases.project(project_id)
    clock = ClockForTest(datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC))
    secrets: Any = None
    events = SqliteEventLog(databases)
    adapter = FakeTrackingAdapter("mlflow")
    adapters: dict[SourceType, FakeTrackingAdapter] = {"mlflow": adapter}
    handoffs = TrackingFakeHandoffs()

    async def _async_token() -> str | None:
        return "fake-token"

    service = TrackingService(
        databases=databases,
        keystore=keystore,
        secrets=secrets,
        events=events,
        adapters=adapters,
        github_token=_async_token,
        clock=clock,
        handoffs=handoffs,
    )

    return TrackingEnv(
        service=service,
        databases=databases,
        keystore=keystore,
        secrets=secrets,
        events=events,
        adapters=adapters,
        project_id=project_id,
        clock=clock,  # type: ignore[arg-type]
        handoffs=handoffs,
    )


async def test_file_source_detects_evaluation_copy_in_results_location(
    tmp_path: Path, project_id: str
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "results.jsonl"

    result_line: dict[str, Any] = {
        "check_id": "chk_1",
        "origin": "agent",
        "ran_at": "2026-10-30T10:00:00Z",
        "fired": False,
        "category": "data_quality",
        "subject": "data",
        "detail": {},
        "source_ref": "ref_1",
    }
    eval_line: dict[str, Any] = {
        "schema_version": 1,
        "evaluation_commit": "c0ffee1",
        "model": "churn_model",
        "version": "1.0.0",
        "run_key": "run_123",
        "gate_config_version": "gate_v1",
        "accuracy": 0.95,
        "decision": "promote",
    }

    content = json.dumps(result_line) + "\n" + json.dumps(eval_line) + "\n"
    target.write_text(content, encoding="utf-8")

    connections = FakeConnections()
    connections.add_local_folder("conn_loc", data_dir)
    reader = FileSourceReader(connections)

    s_id = source_id("file", "*.jsonl", "conn_loc")
    outcome = await reader.read(project_id, s_id, cursor={})

    # Exactly 1 standard result record
    assert len(outcome.records) == 1
    assert outcome.records[0][1].check_id == "chk_1"

    # Exactly 1 evaluation copy detected
    assert len(outcome.evaluations) == 1
    ev = outcome.evaluations[0]
    assert isinstance(ev, EvaluationRecord)
    assert ev.model == "churn_model"
    assert ev.version == "1.0.0"
    assert ev.run_key == "run_123"
    assert ev.schema_version == 1
    assert ev.payload["evaluation_commit"] == "c0ffee1"
    assert ev.payload["accuracy"] == 0.95


async def test_sqlite_evaluation_copies_store_copy_fetch_failed_and_skips_present(
    tracking_env: TrackingEnv,
) -> None:
    env = tracking_env
    # Insert model version with evaluation="fetch_failed"
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )
    p_engine = await env.databases.project(env.project_id)
    t0 = datetime(2026, 10, 30, 9, 0, 0, tzinfo=UTC).isoformat()

    async with p_engine.begin() as conn:
        await conn.execute(
            insert(model_versions).values(
                source_id=source.id,
                model="fraud_detector",
                version="2.0.0",
                run_key="run_failed_eval",
                aliases="[]",
                created_at=t0,
                evaluation="fetch_failed",
            )
        )
        await conn.execute(
            insert(model_versions).values(
                source_id=source.id,
                model="fraud_detector",
                version="1.0.0",
                run_key="run_present_eval",
                aliases="[]",
                created_at=t0,
                evaluation="present",
            )
        )

    copies = SqliteEvaluationCopies(env.databases)

    record_for_failed = EvaluationRecord(
        source_id="",  # SqliteEvaluationCopies matches on (model, version)
        model="fraud_detector",
        version="2.0.0",
        run_key="run_failed_eval",
        schema_version=1,
        payload={"schema_version": 1, "evaluation_commit": "abc1234", "score": 0.92},
        fetched_at=datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC),
    )

    # 1. Stored for fetch_failed version -> returns True
    stored_1 = await copies.store_copy(env.project_id, record_for_failed)
    assert stored_1 is True

    # Check model_versions updated to "present"
    async with p_engine.connect() as conn:
        res = await conn.execute(
            select(model_versions).where(
                model_versions.c.model == "fraud_detector", model_versions.c.version == "2.0.0"
            )
        )
        row = res.mappings().one()
        assert row["evaluation"] == "present"

        # Check evaluations row inserted under source.id
        eval_res = await conn.execute(
            select(evaluations).where(
                evaluations.c.model == "fraud_detector", evaluations.c.version == "2.0.0"
            )
        )
        eval_row = eval_res.mappings().one()
        assert eval_row["source_id"] == source.id
        assert eval_row["schema_version"] == 1
        assert json.loads(eval_row["payload"])["score"] == 0.92

    # 2. Stored once: subsequent attempt returns False
    stored_again = await copies.store_copy(env.project_id, record_for_failed)
    assert stored_again is False

    # 3. Skipped for already "present" version -> returns False
    record_for_present = EvaluationRecord(
        source_id="",
        model="fraud_detector",
        version="1.0.0",
        run_key="run_present_eval",
        schema_version=1,
        payload={"schema_version": 1, "evaluation_commit": "xyz9876"},
        fetched_at=datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC),
    )
    stored_pres = await copies.store_copy(env.project_id, record_for_present)
    assert stored_pres is False


async def test_evaluation_copy_integration_via_results_collect(
    tracking_env: TrackingEnv, tmp_path: Path
) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )
    p_engine = await env.databases.project(env.project_id)
    t0 = datetime(2026, 10, 30, 9, 0, 0, tzinfo=UTC).isoformat()

    async with p_engine.begin() as conn:
        await conn.execute(
            insert(model_versions).values(
                source_id=source.id,
                model="nlp_tagger",
                version="0.1.0",
                run_key="run_nlp",
                aliases="[]",
                created_at=t0,
                evaluation="fetch_failed",
            )
        )

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target_file = data_dir / "results.jsonl"
    eval_line = {
        "schema_version": 1,
        "evaluation_commit": "commit_nlp_1",
        "model": "nlp_tagger",
        "version": "0.1.0",
        "run_key": "run_nlp",
        "f1": 0.88,
    }
    target_file.write_text(json.dumps(eval_line) + "\n", encoding="utf-8")

    connections = FakeConnections()
    connections.add_local_folder("conn_files", data_dir)

    ops = Operations(
        scheduler=None,
        feeds=[],
        on_call=[],
        checks=[],
        results_location=FileResults(connection="conn_files", path_pattern="*.jsonl"),
    )
    payload = HandoffPayload.model_validate(sample_handoff_dict(operations=ops.model_dump()))
    handoff = HandoffView(
        proposal_id="prop_eval",
        version=1,
        approved=datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC),
        payload=payload,
    )
    handoffs = FakeHandoffs(handoff)

    service = ResultsService(
        databases=env.databases,
        handoffs=handoffs,
        connections=connections,
        events=env.events,
        tracking=env.service,
    )

    # Collect results
    now = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
    rep = await service.collect(env.project_id, now)
    assert rep.stored == 0  # Evaluation copies are not result records

    # Query evaluations through TrackingService
    q_eval = await env.service.query(
        env.project_id, TrackingQuery(what="evaluations", model="nlp_tagger")
    )
    assert len(q_eval.rows) == 1
    eval_row = q_eval.rows[0]
    assert isinstance(eval_row, EvaluationRecord)
    assert eval_row.model == "nlp_tagger"
    assert eval_row.version == "0.1.0"
    assert eval_row.payload["f1"] == 0.88
