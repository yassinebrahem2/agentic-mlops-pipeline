"""Tests for Reports and the report route (Task 2, #496)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from mlops_agents.errors import DomainError
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.promotion.constants import REQUIRED_REPORT_FIELDS
from mlops_agents.promotion.contract import (
    EvaluationPayload,
    EvaluationReader,
    EvaluationView,
    ReportDraft,
    ReportField,
)
from mlops_agents.promotion.reports import SqliteReports
from mlops_agents.storage.databases import Databases
from mlops_agents.tracking.contract import EvaluationRecord
from tests.api.conftest import add_local_project, signed_in, started_app


class FakeEvaluationReader(EvaluationReader):
    def __init__(self) -> None:
        self.evaluations: dict[tuple[str, str, str], EvaluationView] = {}

    def set_evaluation(
        self, project_id: str, model: str, version: str, view: EvaluationView
    ) -> None:
        self.evaluations[(project_id, model, version)] = view

    async def for_candidate(
        self, project_id: str, model: str, version: str
    ) -> EvaluationView | None:
        return self.evaluations.get((project_id, model, version))

    async def latest(self, project_id: str, model: str) -> EvaluationView | None:
        for (p, m, _), view in self.evaluations.items():
            if p == project_id and m == model:
                return view
        return None


def _make_sample_evaluation(
    model: str = "churn_predictor", version: str = "1.0.0"
) -> EvaluationView:
    record = EvaluationRecord(
        source_id="src_test",
        model=model,
        version=version,
        run_key="run_1",
        schema_version=1,
        payload={"decision": "promote", "reasons": ["loss improved"]},
        fetched_at=datetime.now(UTC),
    )
    payload = EvaluationPayload(
        schema_version=1,
        evaluation_commit="abc1234",
        gate_config_version="v1",
        decision="promote",
        reasons=["loss improved"],
    )
    return EvaluationView(record=record, payload=payload, not_computed={"parity": None})


def _valid_assessments() -> dict[str, ReportField]:
    return {field: ReportField(value=f"Assessment for {field}") for field in REQUIRED_REPORT_FIELDS}


@pytest.fixture
def reader() -> FakeEvaluationReader:
    return FakeEvaluationReader()


@pytest.fixture
def events(databases: Databases) -> SqliteEventLog:
    return SqliteEventLog(databases)


@pytest.fixture
def reports_service(
    databases: Databases, reader: FakeEvaluationReader, events: SqliteEventLog
) -> SqliteReports:
    return SqliteReports(databases, reader, events)


async def test_save_and_get_complete_report(
    reports_service: SqliteReports, reader: FakeEvaluationReader
) -> None:
    project_id = "prj_test"
    ev = _make_sample_evaluation()
    reader.set_evaluation(project_id, "churn_predictor", "1.0.0", ev)

    draft = ReportDraft(
        model="churn_predictor",
        candidate_version="1.0.0",
        fix_id="fix_1",
        training_request_id="trq_1",
        assessments=_valid_assessments(),
    )

    saved = await reports_service.save(project_id, draft, author="retraining")
    assert saved.id.startswith("rep_")
    assert saved.author == "retraining"
    assert saved.draft.model == "churn_predictor"
    assert saved.draft.candidate_version == "1.0.0"
    assert saved.draft.fix_id == "fix_1"
    assert saved.draft.training_request_id == "trq_1"
    assert len(saved.draft.assessments) == len(REQUIRED_REPORT_FIELDS)
    assert saved.evaluation is not None
    assert saved.evaluation.payload.decision == "promote"

    # Read back via get
    retrieved = await reports_service.get(project_id, saved.id)
    assert retrieved is not None
    assert retrieved.id == saved.id
    assert retrieved.author == saved.author
    assert retrieved.draft.model == saved.draft.model
    assert retrieved.draft.candidate_version == saved.draft.candidate_version
    assert retrieved.draft.fix_id == saved.draft.fix_id
    assert retrieved.draft.training_request_id == saved.draft.training_request_id
    assert retrieved.draft.assessments == saved.draft.assessments
    assert retrieved.evaluation is not None
    assert retrieved.evaluation.payload.decision == "promote"


async def test_save_missing_required_fields_refused(reports_service: SqliteReports) -> None:
    project_id = "prj_test"

    # Missing single field: rollback_plan
    assessments_without_rollback = {
        k: v for k, v in _valid_assessments().items() if k != "rollback_plan"
    }
    draft1 = ReportDraft(
        model="churn",
        candidate_version="v1",
        assessments=assessments_without_rollback,
    )
    with pytest.raises(DomainError) as exc_info1:
        await reports_service.save(project_id, draft1, author="retraining")
    assert exc_info1.value.status == 422
    assert "rollback_plan" in exc_info1.value.message

    # Missing multiple fields: leakage, imbalance
    assessments_without_two = {
        k: v for k, v in _valid_assessments().items() if k not in ("leakage", "imbalance")
    }
    draft2 = ReportDraft(
        model="churn",
        candidate_version="v1",
        assessments=assessments_without_two,
    )
    with pytest.raises(DomainError) as exc_info2:
        await reports_service.save(project_id, draft2, author="retraining")
    assert exc_info2.value.status == 422
    assert "leakage" in exc_info2.value.message
    assert "imbalance" in exc_info2.value.message

    # Empty assessments: all required fields named
    draft_empty = ReportDraft(
        model="churn",
        candidate_version="v1",
        assessments={},
    )
    with pytest.raises(DomainError) as exc_info_empty:
        await reports_service.save(project_id, draft_empty, author="retraining")
    assert exc_info_empty.value.status == 422
    for field in REQUIRED_REPORT_FIELDS:
        assert field in exc_info_empty.value.message


async def test_report_saved_event_recorded_once(
    reports_service: SqliteReports, events: SqliteEventLog
) -> None:
    project_id = "prj_test"
    draft = ReportDraft(
        model="churn_predictor",
        candidate_version="1.0.0",
        assessments=_valid_assessments(),
    )

    saved = await reports_service.save(project_id, draft, author="retraining")

    page = await events.page(project_id, types=["report.saved"])
    assert len(page) == 1
    event = page[0]
    assert event.type == "report.saved"
    assert event.subject_id == saved.id
    assert event.source == "retraining"
    assert event.payload["report_id"] == saved.id
    assert event.payload["model"] == "churn_predictor"
    assert event.payload["candidate_version"] == "1.0.0"


async def test_for_candidate_returns_newest(reports_service: SqliteReports) -> None:
    project_id = "prj_test"
    draft1 = ReportDraft(
        model="churn",
        candidate_version="v2",
        assessments=_valid_assessments(),
    )
    saved1 = await reports_service.save(project_id, draft1, author="retraining")

    draft2 = ReportDraft(
        model="churn",
        candidate_version="v2",
        assessments=_valid_assessments(),
    )
    saved2 = await reports_service.save(project_id, draft2, author="retraining")

    latest = await reports_service.for_candidate(project_id, "churn", "v2")
    assert latest is not None
    assert latest.id == saved2.id
    assert latest.id != saved1.id

    unknown = await reports_service.for_candidate(project_id, "churn", "unknown_version")
    assert unknown is None


async def test_evaluation_stored_after_report_shows_on_next_read(
    reports_service: SqliteReports, reader: FakeEvaluationReader
) -> None:
    project_id = "prj_test"
    draft = ReportDraft(
        model="churn",
        candidate_version="v1",
        assessments=_valid_assessments(),
    )

    # Initial save: reader has no evaluation for candidate
    saved = await reports_service.save(project_id, draft, author="retraining")
    assert saved.evaluation is None

    # Later, an evaluation is recorded
    ev = _make_sample_evaluation("churn", "v1")
    reader.set_evaluation(project_id, "churn", "v1", ev)

    # get() reads fresh evaluation
    retrieved = await reports_service.get(project_id, saved.id)
    assert retrieved is not None
    assert retrieved.evaluation is not None
    assert retrieved.evaluation.payload.decision == "promote"

    # for_candidate() also reads fresh evaluation
    cand = await reports_service.for_candidate(project_id, "churn", "v1")
    assert cand is not None
    assert cand.evaluation is not None
    assert cand.evaluation.payload.decision == "promote"


async def test_get_unknown_report_returns_none(reports_service: SqliteReports) -> None:
    res = await reports_service.get("prj_test", "rep_nonexistent")
    assert res is None


async def test_report_route_200_and_404(tmp_path: Path, git_env: dict[str, str]) -> None:
    async with (
        started_app(tmp_path / "app_data") as app,
        signed_in(app, "http://127.0.0.1:8765") as client,
    ):
        project_id = await add_local_project(app, tmp_path, git_env)

        # Save a report via services.reports
        reports_svc: SqliteReports = app.state.services.reports
        draft = ReportDraft(
            model="fraud_detector",
            candidate_version="v3.0",
            assessments=_valid_assessments(),
        )
        saved = await reports_svc.save(project_id, draft, author="agent")

        # 200 OK
        resp = await client.get(f"/api/projects/{project_id}/reports/{saved.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == saved.id
        assert data["draft"]["model"] == "fraud_detector"
        assert data["draft"]["candidate_version"] == "v3.0"
        assert data["author"] == "agent"

        # 404 Not Found
        resp_404 = await client.get(f"/api/projects/{project_id}/reports/rep_missing")
        assert resp_404.status_code == 404
        assert resp_404.json()["code"] == "report_not_found"
