"""Tests for EvaluationReader, comparable, and not_computed derivation (Task 1, #495)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from mlops_agents.agents.runtime.contract import TaskView
from mlops_agents.promotion.contract import (
    EvaluationPayload,
    EvaluationView,
    ParityResult,
)
from mlops_agents.promotion.evaluation import SqliteEvaluationReader, comparable
from mlops_agents.tracking.contract import EvaluationRecord, Page, TrackingQuery
from tests.fakes.fake_tasks import FakeTasks


class FakeTracking:
    def __init__(self, records: list[EvaluationRecord] | None = None) -> None:
        self.records: list[EvaluationRecord] = list(records or [])
        self.queries: list[TrackingQuery] = []

    async def query(self, project_id: str, q: TrackingQuery) -> Page[EvaluationRecord]:
        self.queries.append(q)
        matching = [
            r
            for r in self.records
            if (q.model is None or r.model == q.model)
            and (q.version is None or r.version == q.version)
        ]
        return Page(rows=matching, total=len(matching), truncated=False)

    async def sources(self, project_id: str) -> list[Any]:
        return []

    async def create_source(self, project_id: str, type: Any, settings: Any, auth: Any) -> Any:
        raise NotImplementedError

    async def sync(self, project_id: str, source_id: str | None = None) -> None:
        raise NotImplementedError


def _make_record(
    *,
    source_id: str = "src_mlflow",
    model: str = "churn_predictor",
    version: str = "1.0.0",
    run_key: str = "run_abc",
    schema_version: int = 1,
    payload: dict[str, Any] | None = None,
    fetched_at: datetime | None = None,
) -> EvaluationRecord:
    default_payload = {
        "schema_version": 1,
        "evaluation_commit": "c0ffee1",
        "gate_config_version": "gate_v1",
        "parity": {"tolerance": 0.01, "mismatch_rate": 0.005},
        "decision": "promote",
    }
    return EvaluationRecord(
        source_id=source_id,
        model=model,
        version=version,
        run_key=run_key,
        schema_version=schema_version,
        payload=payload if payload is not None else default_payload,
        fetched_at=fetched_at or datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_record_maps_to_view() -> None:
    record = _make_record()
    tracking = FakeTracking([record])
    tasks = FakeTasks([])
    reader = SqliteEvaluationReader(tracking, tasks)

    view = await reader.for_candidate("prj_1", "churn_predictor", "1.0.0")
    assert view is not None
    assert isinstance(view, EvaluationView)
    assert view.record == record
    assert view.payload.evaluation_commit == "c0ffee1"
    assert view.payload.gate_config_version == "gate_v1"
    assert view.payload.decision == "promote"
    assert view.payload.parity == ParityResult(tolerance=0.01, mismatch_rate=0.005)


@pytest.mark.asyncio
async def test_two_sources_pick_newest() -> None:
    older_record = _make_record(
        source_id="src_older",
        payload={
            "schema_version": 1,
            "evaluation_commit": "commit_older",
            "gate_config_version": "gate_v1",
        },
        fetched_at=datetime(2026, 1, 10, 10, 0, tzinfo=UTC),
    )
    newer_record = _make_record(
        source_id="src_newer",
        payload={
            "schema_version": 1,
            "evaluation_commit": "commit_newer",
            "gate_config_version": "gate_v1",
        },
        fetched_at=datetime(2026, 1, 20, 10, 0, tzinfo=UTC),
    )

    # Test when older is first in tracking rows
    tracking_1 = FakeTracking([older_record, newer_record])
    reader_1 = SqliteEvaluationReader(tracking_1, FakeTasks([]))
    view_1 = await reader_1.for_candidate("prj_1", "churn_predictor", "1.0.0")
    assert view_1 is not None
    assert view_1.record.source_id == "src_newer"
    assert view_1.payload.evaluation_commit == "commit_newer"

    # Test when newer is first in tracking rows
    tracking_2 = FakeTracking([newer_record, older_record])
    reader_2 = SqliteEvaluationReader(tracking_2, FakeTasks([]))
    view_2 = await reader_2.for_candidate("prj_1", "churn_predictor", "1.0.0")
    assert view_2 is not None
    assert view_2.record.source_id == "src_newer"
    assert view_2.payload.evaluation_commit == "commit_newer"


@pytest.mark.asyncio
async def test_payload_validation_failures(caplog: pytest.LogCaptureFixture) -> None:
    # 1. schema_version != 1
    bad_schema_record = _make_record(
        payload={
            "schema_version": 2,
            "evaluation_commit": "commit_1",
            "gate_config_version": "gate_v1",
        }
    )
    tracking = FakeTracking([bad_schema_record])
    reader = SqliteEvaluationReader(tracking, FakeTasks([]))

    with caplog.at_level(logging.WARNING):
        caplog.clear()
        res = await reader.for_candidate("prj_1", "churn_predictor", "1.0.0")
        assert res is None
        # Must log warning once
        assert len(caplog.records) == 1
        assert "churn_predictor" in caplog.records[0].message
        # NO payload text in the log
        assert "commit_1" not in caplog.records[0].message
        assert "gate_v1" not in caplog.records[0].message

        # Second call for the same record must NOT log again
        res2 = await reader.for_candidate("prj_1", "churn_predictor", "1.0.0")
        assert res2 is None
        assert len(caplog.records) == 1

    # 2. Missing required field (e.g. missing evaluation_commit)
    bad_field_record = _make_record(
        source_id="src_missing_field",
        payload={
            "schema_version": 1,
            # missing evaluation_commit
            "gate_config_version": "gate_v1",
        },
    )
    tracking_bad = FakeTracking([bad_field_record])
    reader_bad = SqliteEvaluationReader(tracking_bad, FakeTasks([]))

    with caplog.at_level(logging.WARNING):
        caplog.clear()
        res_bad = await reader_bad.for_candidate("prj_1", "churn_predictor", "1.0.0")
        assert res_bad is None
        assert len(caplog.records) == 1
        assert "churn_predictor" in caplog.records[0].message
        assert "gate_v1" not in caplog.records[0].message


@pytest.mark.asyncio
async def test_extra_team_fields_survive() -> None:
    payload_data = {
        "schema_version": 1,
        "evaluation_commit": "c0ffee1",
        "gate_config_version": "gate_v1",
        "custom_accuracy": 0.942,
        "team_experiment_note": "custom experiment 42",
    }
    record = _make_record(payload=payload_data)
    tracking = FakeTracking([record])
    reader = SqliteEvaluationReader(tracking, FakeTasks([]))

    view = await reader.for_candidate("prj_1", "churn_predictor", "1.0.0")
    assert view is not None
    # Extra fields survive in payload
    assert view.payload.model_extra is not None
    assert view.payload.model_extra["custom_accuracy"] == 0.942
    assert view.payload.model_extra["team_experiment_note"] == "custom experiment 42"


@pytest.mark.asyncio
async def test_not_computed_lists_none_fields_and_matches_prs() -> None:
    # Payload provides parity and decision; other optional fields are None
    payload_data = {
        "schema_version": 1,
        "evaluation_commit": "c0ffee1",
        "gate_config_version": "gate_v1",
        "parity": {"tolerance": 0.05, "mismatch_rate": 0.01},
        "decision": "promote",
    }
    record = _make_record(payload=payload_data)
    tracking = FakeTracking([record])

    open_tasks = [
        # Matching deployment task with underscore replaced by space
        TaskView(
            id="tsk_1",
            agent="deployment",
            title="Implement expected cost calculation",
            branch="feat/cost",
            status="active",
            review_kind=None,
            pr_number=101,
            pr_url="https://github.com/org/repo/pull/101",
        ),
        # Matching deployment task with exact field name in title
        TaskView(
            id="tsk_2",
            agent="deployment",
            title="Add decision_rate check",
            branch="feat/decision-rate",
            status="active",
            review_kind=None,
            pr_number=102,
            pr_url="https://github.com/org/repo/pull/102",
        ),
        # Matching title, but agent is NOT deployment -> should be ignored
        TaskView(
            id="tsk_3",
            agent="retraining",
            title="Compute calibration scores",
            branch="feat/cal",
            status="active",
            review_kind=None,
            pr_number=103,
            pr_url="https://github.com/org/repo/pull/103",
        ),
        # Matching title and agent, but pr_number is None -> should NOT match as PR
        TaskView(
            id="tsk_4",
            agent="deployment",
            title="Setup serving compatibility",
            branch="feat/compat",
            status="active",
            review_kind=None,
            pr_number=None,
            pr_url=None,
        ),
    ]
    tasks = FakeTasks(open_tasks)
    reader = SqliteEvaluationReader(tracking, tasks)

    view = await reader.for_candidate("prj_1", "churn_predictor", "1.0.0")
    assert view is not None

    not_comp = view.not_computed

    # Provided fields must NOT be in not_computed
    assert "parity" not in not_comp
    assert "decision" not in not_comp
    assert "schema_version" not in not_comp
    assert "evaluation_commit" not in not_comp
    assert "gate_config_version" not in not_comp

    # None optional fields must be present in not_computed
    assert not_comp["expected_cost"] == "101"
    assert not_comp["decision_rate"] == "102"
    assert not_comp["calibration"] is None  # agent was retraining
    assert not_comp["serving_compatibility"] is None  # pr_number was None
    assert not_comp["champion_metrics"] is None
    assert not_comp["challenger_metrics"] is None
    assert not_comp["holdout_window"] is None
    assert not_comp["training_window"] is None
    assert not_comp["computed_where"] is None
    assert not_comp["champion_version"] is None

    # Every key in not_computed must be an optional field of EvaluationPayload
    for field_name in not_comp:
        field_info = EvaluationPayload.model_fields[field_name]
        assert not field_info.is_required()
        assert getattr(view.payload, field_name) is None


@pytest.mark.asyncio
async def test_latest_picks_newest() -> None:
    rec_v1 = _make_record(
        model="fraud_model",
        version="1.0.0",
        fetched_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    )
    rec_v2 = _make_record(
        model="fraud_model",
        version="2.0.0",
        fetched_at=datetime(2026, 1, 15, 10, 0, tzinfo=UTC),
    )
    rec_other = _make_record(
        model="other_model",
        version="1.0.0",
        fetched_at=datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
    )

    tracking = FakeTracking([rec_v1, rec_v2, rec_other])
    reader = SqliteEvaluationReader(tracking, FakeTasks([]))

    view = await reader.latest("prj_1", "fraud_model")
    assert view is not None
    assert view.record.version == "2.0.0"
    assert view.record.model == "fraud_model"

    # Query called with what="evaluations", model="fraud_model"
    assert len(tracking.queries) == 1
    assert tracking.queries[0].what == "evaluations"
    assert tracking.queries[0].model == "fraud_model"
    assert tracking.queries[0].version is None

    # Unknown model gives None
    view_none = await reader.latest("prj_1", "unknown_model")
    assert view_none is None


@pytest.mark.asyncio
async def test_candidate_not_found_returns_none() -> None:
    tracking = FakeTracking([])
    reader = SqliteEvaluationReader(tracking, FakeTasks([]))
    view = await reader.for_candidate("prj_1", "nonexistent", "1.0.0")
    assert view is None


def test_comparable() -> None:
    base = EvaluationPayload(
        schema_version=1,
        evaluation_commit="commit_abc",
        gate_config_version="v1.0",
        decision="promote",
        expected_cost=10.0,
    )

    same_commits_same_gate = EvaluationPayload(
        schema_version=1,
        evaluation_commit="commit_abc",
        gate_config_version="v1.0",
        decision="reject",
        expected_cost=99.9,
    )
    assert comparable(base, same_commits_same_gate) is True

    diff_commit = EvaluationPayload(
        schema_version=1,
        evaluation_commit="commit_xyz",
        gate_config_version="v1.0",
    )
    assert comparable(base, diff_commit) is False

    diff_gate = EvaluationPayload(
        schema_version=1,
        evaluation_commit="commit_abc",
        gate_config_version="v2.0",
    )
    assert comparable(base, diff_gate) is False

    diff_both = EvaluationPayload(
        schema_version=1,
        evaluation_commit="commit_xyz",
        gate_config_version="v2.0",
    )
    assert comparable(base, diff_both) is False
