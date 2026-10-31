"""Tests for team_gate mode observer (Task 5, #499)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from sqlalchemy import select

from mlops_agents.api.services import Services
from mlops_agents.clock import to_iso
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.hooks.contract import Hooks
from mlops_agents.notifications.contract import (
    Message,
    NotificationKind,
    Notifier,
    OnCallPeople,
    OnCallPerson,
)
from mlops_agents.paths import DataPaths
from mlops_agents.promotion.contract import (
    EvaluationPayload,
    EvaluationReader,
    EvaluationView,
    ReportDraft,
    ReportField,
    Reports,
    ReportView,
)
from mlops_agents.promotion.promotions import SqlitePromotions
from mlops_agents.promotion.team_gate import CURSOR_KEY, TeamGateObserver
from mlops_agents.proposals.contract import Handoffs, HandoffView
from mlops_agents.proposals.payloads import Contact, HandoffPayload, OnCallHours
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import project_settings
from mlops_agents.tracking.contract import (
    EvaluationRecord,
    ModelVersionRow,
    Page,
    RunRow,
    Tracking,
    TrackingQuery,
)
from mlops_agents.training.constants import REQUEST_TAG
from tests.api.conftest import started_app
from tests.proposals.samples import handoff as sample_handoff
from tests.proposals.samples import model as sample_model

PROJECT_ID = "prj_teamgatetest"


class FailingHooks:
    """Hooks double that fails if any method is called."""

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Hooks.get must not be called in team_gate mode")

    async def list(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Hooks.list must not be called in team_gate mode")

    async def runs(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Hooks.runs must not be called in team_gate mode")

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Hooks.run must not be called in team_gate mode")


class RecordingNotifier(Notifier):
    """Notifier double that records all notify calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def notify(
        self,
        project_id: str,
        person: OnCallPerson,
        message: Message,
        *,
        kind: NotificationKind,
        incident_id: str | None,
    ) -> Literal["sent", "retrying", "failed"]:
        self.calls.append(
            {
                "project_id": project_id,
                "person": person,
                "message": message,
                "kind": kind,
                "incident_id": incident_id,
            }
        )
        return "sent"


class FakeOnCallPeople(OnCallPeople):
    def __init__(self, people: list[OnCallPerson] | None = None) -> None:
        self.people = list(people or [])

    async def list(self, project_id: str) -> list[OnCallPerson]:
        return list(self.people)

    async def replace(self, project_id: str, people: list[Any]) -> list[OnCallPerson]:
        return list(self.people)


class FakeTracking(Tracking):
    def __init__(self) -> None:
        self.versions: list[ModelVersionRow] = []
        self.runs: list[RunRow] = []

    async def sources(self, project_id: str) -> list[Any]:
        return []

    async def create_source(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def sync(self, project_id: str, source_id: str | None = None) -> None:
        pass

    async def query(self, project_id: str, q: TrackingQuery) -> Page[Any]:
        if q.what == "versions":
            rows = [v for v in self.versions if q.model is None or v.model == q.model]
            return Page(rows=rows, total=len(rows), truncated=False)
        elif q.what == "runs":
            rows = [r for r in self.runs if q.run_key is None or r.key == q.run_key]
            return Page(rows=rows, total=len(rows), truncated=False)
        return Page(rows=[], total=0, truncated=False)


class FakeHandoffs(Handoffs):
    def __init__(self, handoff: HandoffView | None = None) -> None:
        self._handoff = handoff

    async def current(self, project_id: str) -> HandoffView | None:
        return self._handoff

    async def store(self, project_id: str, proposal_id: str, payload: Any) -> int:
        raise NotImplementedError

    def add_observer(self, observer: Any) -> None:
        pass


class FakeEvaluationReader(EvaluationReader):
    def __init__(self, views: list[EvaluationView] | None = None) -> None:
        self.views = list(views or [])

    async def for_candidate(
        self, project_id: str, model: str, version: str
    ) -> EvaluationView | None:
        return next(
            (v for v in self.views if v.record.model == model and v.record.version == version),
            None,
        )

    async def latest(self, project_id: str, model: str) -> EvaluationView | None:
        return next((v for v in self.views if v.record.model == model), None)


class FakeReports(Reports):
    def __init__(self, reports: list[ReportView] | None = None) -> None:
        self.reports = list(reports or [])

    async def save(self, project_id: str, draft: ReportDraft, author: str) -> ReportView:
        raise NotImplementedError

    async def get(self, project_id: str, report_id: str) -> ReportView | None:
        return next((r for r in self.reports if r.id == report_id), None)

    async def for_candidate(self, project_id: str, model: str, version: str) -> ReportView | None:
        return next(
            (
                r
                for r in self.reports
                if r.draft.model == model and r.draft.candidate_version == version
            ),
            None,
        )


def make_on_call_person(
    id: str = "prs_1", name: str = "Alice", escalation_order: int = 1
) -> OnCallPerson:
    return OnCallPerson(
        id=id,
        name=name,
        contacts=[Contact(channel="email", address=f"{name.lower()}@example.com")],
        hours=OnCallHours(days=["mon", "tue", "wed", "thu", "fri"], start="09:00", end="17:00"),
        timezone="UTC",
        escalation_order=escalation_order,
    )


def make_handoff(
    models: list[dict[str, Any]] | None = None,
) -> HandoffView:
    if models is None:
        models = [
            sample_model(
                "fraud_detection",
                promotion="team_gate",
                environments={"production": "production"},
                production_signal={"kind": "tracker_alias", "ref": "production"},
            )
        ]
    raw = sample_handoff(models=models)
    payload = HandoffPayload.model_validate(raw)
    return HandoffView(
        proposal_id="prp_handoff_1",
        version=1,
        approved=datetime.now(UTC),
        payload=payload,
    )


def make_eval_view(
    model: str,
    version: str,
    run_key: str,
    decision: Literal["promote", "hold", "reject"] | None = "promote",
    reasons: list[str] | None = None,
    champion_version: str | None = "v0",
) -> EvaluationView:
    now = datetime.now(UTC)
    record = EvaluationRecord(
        source_id="src_1",
        model=model,
        version=version,
        run_key=run_key,
        schema_version=1,
        payload={},
        fetched_at=now,
    )
    payload = EvaluationPayload(
        schema_version=1,
        evaluation_commit="commit_1",
        gate_config_version="cfg_1",
        champion_version=champion_version,
        decision=decision,
        reasons=reasons,
    )
    return EvaluationView(
        record=record,
        payload=payload,
        not_computed={},
    )


def make_report_view(model: str, version: str, report_id: str = "rep_1") -> ReportView:
    draft = ReportDraft(
        model=model,
        candidate_version=version,
        assessments={"accuracy": ReportField(value="0.95")},
    )
    return ReportView(
        id=report_id,
        draft=draft,
        evaluation=None,
        created=datetime.now(UTC),
        author="agent",
    )


@pytest.fixture
async def databases(tmp_path: Path):
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    dbs = Databases(paths)
    try:
        yield dbs
    finally:
        await dbs.dispose()


@pytest.mark.anyio
async def test_first_run_seeds_cursor(databases: Databases) -> None:
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    tracking = FakeTracking()
    notifier = RecordingNotifier()
    on_call = FakeOnCallPeople([make_on_call_person()])
    reader = FakeEvaluationReader()
    reports = FakeReports()
    events = SqliteEventLog(databases)
    promotions_service = SqlitePromotions(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        reader=reader,
        reports=reports,
        hooks=cast(Hooks, FailingHooks()),
        events=events,
    )

    observer = TeamGateObserver(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        reader=reader,
        reports=reports,
        promotions=promotions_service,
        notifier=notifier,
        on_call=on_call,
    )

    await observer.synced(PROJECT_ID, now=t0)

    # Check cursor was set to t0
    engine = await databases.project(PROJECT_ID)
    async with engine.connect() as conn:
        val = (
            await conn.execute(
                select(project_settings.c.value).where(project_settings.c.key == CURSOR_KEY)
            )
        ).scalar_one_or_none()
    assert val == to_iso(t0)
    assert len(notifier.calls) == 0


@pytest.mark.anyio
async def test_promote_decision_records_applied_and_notifies(databases: Databases) -> None:
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    model = "fraud_detection"
    candidate_version = "v2"
    run_key = "run_123"

    # Pre-set cursor
    engine = await databases.project(PROJECT_ID)
    async with engine.begin() as conn:
        await conn.execute(project_settings.insert().values(key=CURSOR_KEY, value=to_iso(t0)))

    tracking = FakeTracking()
    tracking.versions.append(
        ModelVersionRow(
            source_id="src_1",
            model=model,
            version=candidate_version,
            run_key=run_key,
            aliases=[],
            created=t1,
            evaluation="present",
        )
    )
    tracking.runs.append(
        RunRow(
            source_id="src_1",
            key=run_key,
            experiment="exp_1",
            status="succeeded",
            start=t1 - timedelta(minutes=30),
            end=t1,
            params={},
            tags={REQUEST_TAG: "tr_req_1"},
            git_commit="sha123",
            datasets=[],
        )
    )

    reader = FakeEvaluationReader(
        [
            make_eval_view(
                model, candidate_version, run_key, decision="promote", champion_version="v1"
            )
        ]
    )
    reports = FakeReports([make_report_view(model, candidate_version, "rep_test_1")])
    notifier = RecordingNotifier()
    person = make_on_call_person("prs_1", "Alice", escalation_order=1)
    on_call = FakeOnCallPeople([person])
    events = SqliteEventLog(databases)
    promotions_service = SqlitePromotions(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        reader=reader,
        reports=reports,
        hooks=cast(Hooks, FailingHooks()),
        events=events,
    )

    observer = TeamGateObserver(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        reader=reader,
        reports=reports,
        promotions=promotions_service,
        notifier=notifier,
        on_call=on_call,
    )

    await observer.synced(PROJECT_ID, now=t1 + timedelta(seconds=1))

    # 1 promotion recorded: applied, mode=team_gate, decided_by=team_gate
    history = await promotions_service.history(PROJECT_ID, model=model)
    assert len(history) == 1
    promo = history[0]
    assert promo.model == model
    assert promo.candidate_version == candidate_version
    assert promo.champion_version == "v1"
    assert promo.mode == "team_gate"
    assert promo.decided_by == "team_gate"
    assert promo.status == "applied"
    assert promo.applied_at is not None
    assert promo.report_id == "rep_test_1"
    assert promo.refusals == []

    # 1 notification sent to first on-call person
    assert len(notifier.calls) == 1
    call = notifier.calls[0]
    assert call["project_id"] == PROJECT_ID
    assert call["person"].id == "prs_1"
    assert call["kind"] == "promotion"
    assert call["incident_id"] is None
    msg: Message = call["message"]
    assert msg.severity == "low"
    assert msg.urgent is False
    assert "promote" in msg.title.lower()

    # 1 event recorded
    evs = await events.history(PROJECT_ID, types=["promotion.applied"])
    assert len(evs) == 1
    assert evs[0].subject_id == promo.id


@pytest.mark.anyio
async def test_reject_and_hold_record_refused_with_reasons(databases: Databases) -> None:
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    t2 = t0 + timedelta(minutes=10)
    model = "fraud_detection"

    engine = await databases.project(PROJECT_ID)
    async with engine.begin() as conn:
        await conn.execute(project_settings.insert().values(key=CURSOR_KEY, value=to_iso(t0)))

    tracking = FakeTracking()
    # Candidate 1: reject
    tracking.versions.append(
        ModelVersionRow(
            source_id="src_1",
            model=model,
            version="v_reject",
            run_key="run_reject",
            aliases=[],
            created=t1,
            evaluation="present",
        )
    )
    tracking.runs.append(
        RunRow(
            source_id="src_1",
            key="run_reject",
            experiment="exp_1",
            status="succeeded",
            start=t1 - timedelta(minutes=20),
            end=t1,
            params={},
            tags={REQUEST_TAG: "tr_req_rej"},
            git_commit="sha123",
            datasets=[],
        )
    )
    # Candidate 2: hold
    tracking.versions.append(
        ModelVersionRow(
            source_id="src_1",
            model=model,
            version="v_hold",
            run_key="run_hold",
            aliases=[],
            created=t2,
            evaluation="present",
        )
    )
    tracking.runs.append(
        RunRow(
            source_id="src_1",
            key="run_hold",
            experiment="exp_1",
            status="succeeded",
            start=t2 - timedelta(minutes=20),
            end=t2,
            params={},
            tags={REQUEST_TAG: "tr_req_hold"},
            git_commit="sha123",
            datasets=[],
        )
    )

    reject_reasons = ["Latency exceeded 200ms", "Accuracy drop 2%"]
    hold_reasons = ["Calibration check pending review"]
    reader = FakeEvaluationReader(
        [
            make_eval_view(
                model, "v_reject", "run_reject", decision="reject", reasons=reject_reasons
            ),
            make_eval_view(model, "v_hold", "run_hold", decision="hold", reasons=hold_reasons),
        ]
    )
    reports = FakeReports()
    notifier = RecordingNotifier()
    person = make_on_call_person()
    on_call = FakeOnCallPeople([person])
    events = SqliteEventLog(databases)
    promotions_service = SqlitePromotions(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        reader=reader,
        reports=reports,
        hooks=cast(Hooks, FailingHooks()),
        events=events,
    )

    observer = TeamGateObserver(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        reader=reader,
        reports=reports,
        promotions=promotions_service,
        notifier=notifier,
        on_call=on_call,
    )

    await observer.synced(PROJECT_ID, now=t2 + timedelta(seconds=1))

    # 2 promotions recorded: both refused with gate reasons in refusals
    history = await promotions_service.history(PROJECT_ID, model=model)
    assert len(history) == 2

    hold_promo = next(p for p in history if p.candidate_version == "v_hold")
    assert hold_promo.status == "refused"
    assert hold_promo.applied_at is None
    assert hold_promo.refusals == hold_reasons

    reject_promo = next(p for p in history if p.candidate_version == "v_reject")
    assert reject_promo.status == "refused"
    assert reject_promo.applied_at is None
    assert reject_promo.refusals == reject_reasons

    # 2 notifications sent with reasons in message lines
    assert len(notifier.calls) == 2
    for call in notifier.calls:
        msg = call["message"]
        assert msg.severity == "low"
        assert msg.urgent is False
        if "v_reject" in msg.title:
            for r in reject_reasons:
                assert any(r in line for line in msg.lines)
        if "v_hold" in msg.title:
            for r in hold_reasons:
                assert any(r in line for line in msg.lines)

    # 2 promotion.refused events
    evs = await events.history(PROJECT_ID, types=["promotion.refused"])
    assert len(evs) == 2


@pytest.mark.anyio
async def test_run_without_request_tag_records_nothing(databases: Databases) -> None:
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    model = "fraud_detection"
    candidate_version = "v_team"
    run_key = "run_team"

    engine = await databases.project(PROJECT_ID)
    async with engine.begin() as conn:
        await conn.execute(project_settings.insert().values(key=CURSOR_KEY, value=to_iso(t0)))

    tracking = FakeTracking()
    tracking.versions.append(
        ModelVersionRow(
            source_id="src_1",
            model=model,
            version=candidate_version,
            run_key=run_key,
            aliases=[],
            created=t1,
            evaluation="present",
        )
    )
    # Team's own run: NO REQUEST_TAG
    tracking.runs.append(
        RunRow(
            source_id="src_1",
            key=run_key,
            experiment="exp_1",
            status="succeeded",
            start=t1 - timedelta(minutes=30),
            end=t1,
            params={},
            tags={"team": "ml_team"},
            git_commit="sha123",
            datasets=[],
        )
    )

    reader = FakeEvaluationReader(
        [make_eval_view(model, candidate_version, run_key, decision="promote")]
    )
    notifier = RecordingNotifier()
    events = SqliteEventLog(databases)
    promotions_service = SqlitePromotions(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        reader=reader,
        reports=FakeReports(),
        hooks=cast(Hooks, FailingHooks()),
        events=events,
    )

    observer = TeamGateObserver(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        reader=reader,
        reports=FakeReports(),
        promotions=promotions_service,
        notifier=notifier,
        on_call=FakeOnCallPeople([make_on_call_person()]),
    )

    await observer.synced(PROJECT_ID, now=t1 + timedelta(seconds=1))

    # Nothing recorded, no notification sent
    history = await promotions_service.history(PROJECT_ID, model=model)
    assert len(history) == 0
    assert len(notifier.calls) == 0


@pytest.mark.anyio
async def test_second_sync_adds_nothing(databases: Databases) -> None:
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    model = "fraud_detection"
    candidate_version = "v2"
    run_key = "run_123"

    engine = await databases.project(PROJECT_ID)
    async with engine.begin() as conn:
        await conn.execute(project_settings.insert().values(key=CURSOR_KEY, value=to_iso(t0)))

    tracking = FakeTracking()
    tracking.versions.append(
        ModelVersionRow(
            source_id="src_1",
            model=model,
            version=candidate_version,
            run_key=run_key,
            aliases=[],
            created=t1,
            evaluation="present",
        )
    )
    tracking.runs.append(
        RunRow(
            source_id="src_1",
            key=run_key,
            experiment="exp_1",
            status="succeeded",
            start=t1 - timedelta(minutes=30),
            end=t1,
            params={},
            tags={REQUEST_TAG: "tr_req_1"},
            git_commit="sha123",
            datasets=[],
        )
    )

    reader = FakeEvaluationReader(
        [make_eval_view(model, candidate_version, run_key, decision="promote")]
    )
    notifier = RecordingNotifier()
    events = SqliteEventLog(databases)
    promotions_service = SqlitePromotions(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        reader=reader,
        reports=FakeReports(),
        hooks=cast(Hooks, FailingHooks()),
        events=events,
    )

    observer = TeamGateObserver(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        reader=reader,
        reports=FakeReports(),
        promotions=promotions_service,
        notifier=notifier,
        on_call=FakeOnCallPeople([make_on_call_person()]),
    )

    # First sync
    await observer.synced(PROJECT_ID, now=t1 + timedelta(seconds=1))
    assert len(await promotions_service.history(PROJECT_ID, model=model)) == 1
    assert len(notifier.calls) == 1

    # Second sync with same data
    await observer.synced(PROJECT_ID, now=t1 + timedelta(minutes=10))
    assert len(await promotions_service.history(PROJECT_ID, model=model)) == 1
    assert len(notifier.calls) == 1


@pytest.mark.anyio
async def test_person_mode_model_ignored(databases: Databases) -> None:
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    model = "fraud_detection"
    candidate_version = "v_person"
    run_key = "run_person"

    engine = await databases.project(PROJECT_ID)
    async with engine.begin() as conn:
        await conn.execute(project_settings.insert().values(key=CURSOR_KEY, value=to_iso(t0)))

    tracking = FakeTracking()
    tracking.versions.append(
        ModelVersionRow(
            source_id="src_1",
            model=model,
            version=candidate_version,
            run_key=run_key,
            aliases=[],
            created=t1,
            evaluation="present",
        )
    )
    tracking.runs.append(
        RunRow(
            source_id="src_1",
            key=run_key,
            experiment="exp_1",
            status="succeeded",
            start=t1 - timedelta(minutes=30),
            end=t1,
            params={},
            tags={REQUEST_TAG: "tr_req_p"},
            git_commit="sha123",
            datasets=[],
        )
    )

    # Model is configured with promotion: "person"
    handoff = make_handoff(
        models=[
            sample_model(
                model,
                promotion="person",
                environments={"production": "production"},
                production_signal={"kind": "tracker_alias", "ref": "production"},
            )
        ]
    )

    reader = FakeEvaluationReader(
        [make_eval_view(model, candidate_version, run_key, decision="promote")]
    )
    notifier = RecordingNotifier()
    events = SqliteEventLog(databases)
    promotions_service = SqlitePromotions(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(handoff),
        reader=reader,
        reports=FakeReports(),
        hooks=cast(Hooks, FailingHooks()),
        events=events,
    )

    observer = TeamGateObserver(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(handoff),
        reader=reader,
        reports=FakeReports(),
        promotions=promotions_service,
        notifier=notifier,
        on_call=FakeOnCallPeople([make_on_call_person()]),
    )

    await observer.synced(PROJECT_ID, now=t1 + timedelta(seconds=1))

    # Person mode model is completely ignored by TeamGateObserver
    assert len(await promotions_service.history(PROJECT_ID, model=model)) == 0
    assert len(notifier.calls) == 0


@pytest.mark.anyio
async def test_no_hook_is_ever_run(databases: Databases) -> None:
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    model = "fraud_detection"
    candidate_version = "v_hooktest"
    run_key = "run_hooktest"

    engine = await databases.project(PROJECT_ID)
    async with engine.begin() as conn:
        await conn.execute(project_settings.insert().values(key=CURSOR_KEY, value=to_iso(t0)))

    tracking = FakeTracking()
    tracking.versions.append(
        ModelVersionRow(
            source_id="src_1",
            model=model,
            version=candidate_version,
            run_key=run_key,
            aliases=[],
            created=t1,
            evaluation="present",
        )
    )
    tracking.runs.append(
        RunRow(
            source_id="src_1",
            key=run_key,
            experiment="exp_1",
            status="succeeded",
            start=t1 - timedelta(minutes=30),
            end=t1,
            params={},
            tags={REQUEST_TAG: "tr_req_hook"},
            git_commit="sha123",
            datasets=[],
        )
    )

    reader = FakeEvaluationReader(
        [make_eval_view(model, candidate_version, run_key, decision="promote")]
    )
    notifier = RecordingNotifier()
    events = SqliteEventLog(databases)
    failing_hooks = FailingHooks()
    promotions_service = SqlitePromotions(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        reader=reader,
        reports=FakeReports(),
        hooks=cast(Hooks, failing_hooks),
        events=events,
    )

    observer = TeamGateObserver(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        reader=reader,
        reports=FakeReports(),
        promotions=promotions_service,
        notifier=notifier,
        on_call=FakeOnCallPeople([make_on_call_person()]),
    )

    # Must succeed without raising AssertionError from FailingHooks
    await observer.synced(PROJECT_ID, now=t1 + timedelta(seconds=1))
    assert len(await promotions_service.history(PROJECT_ID, model=model)) == 1


@pytest.mark.anyio
async def test_evaluation_with_no_decision_waits_for_next_sync(databases: Databases) -> None:
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    model = "fraud_detection"
    candidate_version = "v_waiting"
    run_key = "run_waiting"

    engine = await databases.project(PROJECT_ID)
    async with engine.begin() as conn:
        await conn.execute(project_settings.insert().values(key=CURSOR_KEY, value=to_iso(t0)))

    tracking = FakeTracking()
    tracking.versions.append(
        ModelVersionRow(
            source_id="src_1",
            model=model,
            version=candidate_version,
            run_key=run_key,
            aliases=[],
            created=t1,
            evaluation="present",
        )
    )
    tracking.runs.append(
        RunRow(
            source_id="src_1",
            key=run_key,
            experiment="exp_1",
            status="succeeded",
            start=t1 - timedelta(minutes=30),
            end=t1,
            params={},
            tags={REQUEST_TAG: "tr_req_wait"},
            git_commit="sha123",
            datasets=[],
        )
    )

    # Initial evaluation has decision=None (still waiting)
    reader = FakeEvaluationReader(
        [make_eval_view(model, candidate_version, run_key, decision=None)]
    )
    notifier = RecordingNotifier()
    events = SqliteEventLog(databases)
    promotions_service = SqlitePromotions(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        reader=reader,
        reports=FakeReports(),
        hooks=cast(Hooks, FailingHooks()),
        events=events,
    )

    observer = TeamGateObserver(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        reader=reader,
        reports=FakeReports(),
        promotions=promotions_service,
        notifier=notifier,
        on_call=FakeOnCallPeople([make_on_call_person()]),
    )

    # First sync: decision is None -> waits for next sync, cursor not advanced past t1
    await observer.synced(PROJECT_ID, now=t1 + timedelta(seconds=1))
    assert len(await promotions_service.history(PROJECT_ID, model=model)) == 0
    assert len(notifier.calls) == 0

    # Cursor is still t0 because candidate was waiting
    async with engine.connect() as conn:
        cur = (
            await conn.execute(
                select(project_settings.c.value).where(project_settings.c.key == CURSOR_KEY)
            )
        ).scalar_one_or_none()
    assert cur == to_iso(t0)

    # Now decision arrives: promote
    reader.views = [make_eval_view(model, candidate_version, run_key, decision="promote")]

    # Second sync: now processed!
    await observer.synced(PROJECT_ID, now=t1 + timedelta(minutes=2))
    history = await promotions_service.history(PROJECT_ID, model=model)
    assert len(history) == 1
    assert history[0].status == "applied"
    assert len(notifier.calls) == 1


@pytest.mark.anyio
async def test_notification_sent_to_first_on_call_person(databases: Databases) -> None:
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    model = "fraud_detection"
    candidate_version = "v_order"
    run_key = "run_order"

    engine = await databases.project(PROJECT_ID)
    async with engine.begin() as conn:
        await conn.execute(project_settings.insert().values(key=CURSOR_KEY, value=to_iso(t0)))

    tracking = FakeTracking()
    tracking.versions.append(
        ModelVersionRow(
            source_id="src_1",
            model=model,
            version=candidate_version,
            run_key=run_key,
            aliases=[],
            created=t1,
            evaluation="present",
        )
    )
    tracking.runs.append(
        RunRow(
            source_id="src_1",
            key=run_key,
            experiment="exp_1",
            status="succeeded",
            start=t1 - timedelta(minutes=30),
            end=t1,
            params={},
            tags={REQUEST_TAG: "tr_req_order"},
            git_commit="sha123",
            datasets=[],
        )
    )

    reader = FakeEvaluationReader(
        [make_eval_view(model, candidate_version, run_key, decision="promote")]
    )
    notifier = RecordingNotifier()

    # Two on-call people: Bob (escalation_order=2) and Alice (escalation_order=1)
    bob = make_on_call_person("prs_bob", "Bob", escalation_order=2)
    alice = make_on_call_person("prs_alice", "Alice", escalation_order=1)
    on_call = FakeOnCallPeople([bob, alice])

    events = SqliteEventLog(databases)
    promotions_service = SqlitePromotions(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        reader=reader,
        reports=FakeReports(),
        hooks=cast(Hooks, FailingHooks()),
        events=events,
    )

    observer = TeamGateObserver(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        reader=reader,
        reports=FakeReports(),
        promotions=promotions_service,
        notifier=notifier,
        on_call=on_call,
    )

    await observer.synced(PROJECT_ID, now=t1 + timedelta(seconds=1))

    assert len(notifier.calls) == 1
    # Must be sent to Alice (escalation_order=1)
    assert notifier.calls[0]["person"].id == "prs_alice"


@pytest.mark.anyio
async def test_services_wires_team_gate_observer(tmp_path: Path) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        assert any(isinstance(o, TeamGateObserver) for o in services.scheduler.sync_observers)
