"""MLflow records of agent checks registered with RegisterCheck (bug #628)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from mlops_agents.agents.pr_sync import PrChange
from mlops_agents.monitoring.expectations.checks import SqliteChecks
from mlops_agents.monitoring.expectations.service import ExpectationsService
from mlops_agents.monitoring.results.contract import ResultQuery, ResultRecord
from mlops_agents.monitoring.results.mlflow_source import MlflowSourceReader
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.monitoring.settings import MonitoringSettings, set_monitoring_settings
from mlops_agents.monitoring.verdicts.store import is_label_evidence
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import Cadence, CheckSpec, MlflowResults
from mlops_agents.tracking.contract import MetricPoint, ModelVersionRow, RunRow, SyncResult
from tests.monitoring.results.conftest import (
    FakeConnections,
    FakeHandoffs,
    FakeIncidents,
    build_mlflow_handoff,
)
from tests.providers.fake_clock import FakeClock
from tests.tracking.conftest import TrackingEnv

T_REGISTER = datetime(2026, 10, 30, 9, 0, 0, tzinfo=UTC)
T_RUN_1 = datetime(2026, 10, 30, 9, 30, 0, tzinfo=UTC)
T_RUN_2 = datetime(2026, 10, 30, 10, 30, 0, tzinfo=UTC)
T_EVALUATE = datetime(2026, 10, 30, 11, 20, 0, tzinfo=UTC)
COMMIT_RUN_1 = "3f2a9c1"

RECALL = MlflowResults(metric="recall", threshold=0.8, fires_when="below")
HANDOFF_CHECKS = [
    CheckSpec(
        check_id="chk_acc",
        name="Accuracy Check",
        category="model_performance",
        cadence=Cadence(every_minutes=60, cron=None),
        timezone="UTC",
        results=MlflowResults(metric="accuracy", threshold=0.85, fires_when="below"),
    )
]
HANDOFF_LOCATION = MlflowResults(metric="loss", threshold=1.0, fires_when="above")


class _Maintenance:
    async def suspended(self, project_id: str, scope: Any, at: datetime) -> bool:
        return False


def _run(env_source: str, key: str, at: datetime, git_commit: str | None) -> RunRow:
    return RunRow(
        source_id=env_source,
        key=key,
        experiment="exp_1",
        status="succeeded",
        start=at,
        end=at,
        params={},
        tags={},
        git_commit=git_commit,
        datasets=[],
    )


def _point(env_source: str, run_key: str, metric: str, value: float, at: datetime) -> MetricPoint:
    return MetricPoint(
        source_id=env_source, run_key=run_key, metric=metric, step=1, value=value, time=at
    )


async def _seed_mlflow(env: TrackingEnv) -> None:
    """Two runs, an hour apart, each logging recall, accuracy, loss and an unnamed metric;
    only the first names the git commit it ran."""
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )
    s = source.id
    env.adapters["mlflow"].result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=s,
                model="churn_model",
                version="1.0.0",
                run_key="run_1",
                aliases=[],
                created=T_RUN_1,
                evaluation="absent",
            )
        ],
        runs=[_run(s, "run_1", T_RUN_1, COMMIT_RUN_1), _run(s, "run_2", T_RUN_2, None)],
        metrics=[
            _point(s, "run_1", "recall", 0.7, T_RUN_1),
            _point(s, "run_2", "recall", 0.9, T_RUN_2),
            _point(s, "run_1", "accuracy", 0.8, T_RUN_1),
            _point(s, "run_2", "accuracy", 0.9, T_RUN_2),
            _point(s, "run_1", "loss", 1.5, T_RUN_1),
            _point(s, "run_2", "loss", 0.5, T_RUN_2),
            _point(s, "run_1", "other", 3.0, T_RUN_1),
        ],
        aliases={},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, s)


def _tracker_record(
    *,
    check_id: str,
    run_key: str,
    metric: str,
    value: float,
    at: datetime,
    fired: bool,
    threshold: float | None,
    category: str = "model_performance",
    model: str | None = None,
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "origin": "tracker",
        "ran_at": at,
        "fired": fired,
        "value": value,
        "threshold": threshold,
        "category": category,
        "subject": metric,
        "model": model,
        "detail": {"run_key": run_key, "step": 1, "metric": metric, "value": value},
        "source_ref": f"run:{run_key}",
        "version": None,
        "not_enough_data": False,
    }


# The records the reader gives for metrics no registered check names, as on main before #628.
EXPECTED_UNREGISTERED: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "mlflow:accuracy": [
        (
            "run_1:accuracy:1:2026-10-30T09:30:00+00:00",
            _tracker_record(
                check_id="chk_acc",
                run_key="run_1",
                metric="accuracy",
                value=0.8,
                at=T_RUN_1,
                fired=True,
                threshold=0.85,
                model="churn_model",
            ),
        ),
        (
            "run_2:accuracy:1:2026-10-30T10:30:00+00:00",
            _tracker_record(
                check_id="chk_acc",
                run_key="run_2",
                metric="accuracy",
                value=0.9,
                at=T_RUN_2,
                fired=False,
                threshold=0.85,
            ),
        ),
    ],
    "mlflow:loss": [
        (
            "run_1:loss:1:2026-10-30T09:30:00+00:00",
            _tracker_record(
                check_id="mlflow:loss",
                run_key="run_1",
                metric="loss",
                value=1.5,
                at=T_RUN_1,
                fired=True,
                threshold=1.0,
                model="churn_model",
            ),
        ),
        (
            "run_2:loss:1:2026-10-30T10:30:00+00:00",
            _tracker_record(
                check_id="mlflow:loss",
                run_key="run_2",
                metric="loss",
                value=0.5,
                at=T_RUN_2,
                fired=False,
                threshold=1.0,
            ),
        ),
    ],
    "mlflow:other": [
        (
            "run_1:other:1:2026-10-30T09:30:00+00:00",
            _tracker_record(
                check_id="mlflow:other",
                run_key="run_1",
                metric="other",
                value=3.0,
                at=T_RUN_1,
                fired=False,
                threshold=None,
                model="churn_model",
            ),
        ),
    ],
    "mlflow:recall": [
        (
            "run_1:recall:1:2026-10-30T09:30:00+00:00",
            _tracker_record(
                check_id="mlflow:recall",
                run_key="run_1",
                metric="recall",
                value=0.7,
                at=T_RUN_1,
                fired=False,
                threshold=None,
                model="churn_model",
            ),
        ),
        (
            "run_2:recall:1:2026-10-30T10:30:00+00:00",
            _tracker_record(
                check_id="mlflow:recall",
                run_key="run_2",
                metric="recall",
                value=0.9,
                at=T_RUN_2,
                fired=False,
                threshold=None,
            ),
        ),
    ],
}


def _dumped(records: list[tuple[str, ResultRecord]]) -> list[tuple[str, dict[str, Any]]]:
    return [(key, record.model_dump()) for key, record in records]


def _handoff() -> HandoffView:
    return build_mlflow_handoff(HANDOFF_CHECKS, results_location=HANDOFF_LOCATION)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(T_REGISTER)


@pytest.fixture
def incidents() -> FakeIncidents:
    return FakeIncidents()


@pytest.fixture
def expectations_service(
    tracking_env: TrackingEnv, incidents: FakeIncidents, fake_clock: FakeClock
) -> ExpectationsService:
    return ExpectationsService(
        databases=tracking_env.databases,
        handoffs=FakeHandoffs(),
        connections=FakeConnections(),
        incidents=incidents,
        tracking=None,
        maintenance=_Maintenance(),
        clock=fake_clock,
    )


@pytest.fixture
def checks_store(
    tracking_env: TrackingEnv, expectations_service: ExpectationsService, fake_clock: FakeClock
) -> SqliteChecks:
    return SqliteChecks(
        databases=tracking_env.databases,
        expectations=expectations_service,
        events=tracking_env.events,
        clock=fake_clock,
    )


async def _register_recall(checks: SqliteChecks, project_id: str, *, merge: bool) -> None:
    await checks.register(
        project_id,
        check_id="mp_recall",
        category="model_performance",
        cadence=Cadence(every_minutes=60, cron=None),
        timezone="UTC",
        results_location=RECALL,
        pr_number=7,
        agent="model-performance",
    )
    if merge:
        await checks.changed(
            project_id,
            PrChange(
                number=7,
                state="merged",
                merged_at=T_REGISTER,
                head_branch="feature/recall",
                agent="model-performance",
                files=["recall.py"],
            ),
        )


async def test_unregistered_metrics_give_the_records_main_gives(
    tracking_env: TrackingEnv,
) -> None:
    await _seed_mlflow(tracking_env)
    reader = MlflowSourceReader(tracking=tracking_env.service, handoffs=FakeHandoffs(_handoff()))

    for source_id, expected in EXPECTED_UNREGISTERED.items():
        outcome = await reader.read(tracking_env.project_id, source_id, cursor={})
        assert _dumped(outcome.records) == expected, source_id


async def test_active_registered_mlflow_check_meets_its_expectation_with_its_own_records(
    tracking_env: TrackingEnv,
    checks_store: SqliteChecks,
    expectations_service: ExpectationsService,
    incidents: FakeIncidents,
) -> None:
    env = tracking_env
    await set_monitoring_settings(
        env.databases, env.project_id, MonitoringSettings(check_grace_minutes=15)
    )
    await _seed_mlflow(env)
    results = ResultsService(
        databases=env.databases,
        handoffs=FakeHandoffs(),
        connections=FakeConnections(),
        events=env.events,
        tracking=env.service,
    )
    results.bind_checks(checks_store)
    results.add_observer(expectations_service.results_observer)
    await _register_recall(checks_store, env.project_id, merge=True)

    report = await results.collect(env.project_id, T_EVALUATE)

    assert report.failures == {}
    assert report.by_source["mlflow:recall"] == 2
    await expectations_service.evaluate(env.project_id, T_EVALUATE)
    assert [d for _, d, _ in incidents.signals if d.kind == "silence"] == []

    page = await results.store.query(env.project_id, ResultQuery(check_id="mp_recall"))
    stored = sorted(
        (r for r in page.rows if r.source_id == "mlflow:recall"), key=lambda r: r.ran_at
    )
    # A check's version is the git commit of the run that produced the record (S4 4).
    assert [(r.value, r.fired, r.version) for r in stored] == [
        (0.7, True, COMMIT_RUN_1),
        (0.9, False, None),
    ]
    for record in stored:
        assert record.check_id == "mp_recall"
        assert record.category == "model_performance"
        assert record.origin == "agent"
        assert record.threshold == 0.8
        assert is_label_evidence(record)


async def test_pending_registered_check_does_not_change_the_records(
    tracking_env: TrackingEnv, checks_store: SqliteChecks
) -> None:
    await _seed_mlflow(tracking_env)
    await _register_recall(checks_store, tracking_env.project_id, merge=False)
    reader = MlflowSourceReader(
        tracking=tracking_env.service, handoffs=FakeHandoffs(_handoff()), checks=checks_store
    )

    for source_id, expected in EXPECTED_UNREGISTERED.items():
        outcome = await reader.read(tracking_env.project_id, source_id, cursor={})
        assert _dumped(outcome.records) == expected, source_id


async def test_active_registered_check_leaves_other_metrics_unchanged(
    tracking_env: TrackingEnv, checks_store: SqliteChecks
) -> None:
    await _seed_mlflow(tracking_env)
    await _register_recall(checks_store, tracking_env.project_id, merge=True)
    reader = MlflowSourceReader(
        tracking=tracking_env.service, handoffs=FakeHandoffs(_handoff()), checks=checks_store
    )

    for source_id, expected in EXPECTED_UNREGISTERED.items():
        if source_id == "mlflow:recall":
            continue
        outcome = await reader.read(tracking_env.project_id, source_id, cursor={})
        assert _dumped(outcome.records) == expected, source_id


async def test_registered_check_wins_over_a_later_handoff_naming_its_metric(
    tracking_env: TrackingEnv,
    checks_store: SqliteChecks,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _seed_mlflow(tracking_env)
    await _register_recall(checks_store, tracking_env.project_id, merge=True)
    later_handoff = build_mlflow_handoff(
        [
            CheckSpec(
                check_id="chk_handoff_recall",
                name="Recall from the handoff",
                category="drift",
                cadence=Cadence(every_minutes=30, cron=None),
                timezone="UTC",
                results=MlflowResults(metric="recall", threshold=0.5, fires_when="above"),
            )
        ],
        results_location=MlflowResults(metric="recall", threshold=0.1, fires_when="above"),
    )
    reader = MlflowSourceReader(
        tracking=tracking_env.service, handoffs=FakeHandoffs(later_handoff), checks=checks_store
    )

    with caplog.at_level(logging.WARNING):
        outcome = await reader.read(tracking_env.project_id, "mlflow:recall", cursor={})

    records = [r for _, r in outcome.records]
    assert [(r.value, r.fired, r.version) for r in records] == [
        (0.7, True, COMMIT_RUN_1),
        (0.9, False, None),
    ]
    for record in records:
        assert record.check_id == "mp_recall"
        assert record.category == "model_performance"
        assert record.origin == "agent"
        assert record.threshold == 0.8
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "mp_recall" in warnings[0].getMessage()
    assert "recall" in warnings[0].getMessage()


async def test_failing_registered_checks_read_fails_the_read_and_keeps_the_cursor(
    tracking_env: TrackingEnv,
) -> None:
    class _FailingChecks:
        async def list_active(self, project_id: str) -> list[Any]:
            raise RuntimeError("checks store unavailable")

    await _seed_mlflow(tracking_env)
    reader = MlflowSourceReader(
        tracking=tracking_env.service, handoffs=FakeHandoffs(_handoff()), checks=_FailingChecks()
    )
    cursor = {"since": T_REGISTER.isoformat()}

    outcome = await reader.read(tracking_env.project_id, "mlflow:recall", cursor=cursor)

    assert outcome.records == []
    assert outcome.cursor == cursor
    assert outcome.consecutive_failures == 1
    assert outcome.last_error is not None
    assert "checks store unavailable" in outcome.last_error
