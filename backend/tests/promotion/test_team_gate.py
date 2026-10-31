"""Tests for team_gate mode observer (Task 5, #499)."""

from __future__ import annotations

import json
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
from mlops_agents.storage.tables import promotions as promotions_table
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


class ModelFailingTracking(FakeTracking):
    """FakeTracking whose versions query fails for one model, as a tracking store outage would."""

    def __init__(self, failing_model: str | None) -> None:
        super().__init__()
        self.failing_model = failing_model

    async def query(self, project_id: str, q: TrackingQuery) -> Page[Any]:
        if q.what == "versions" and q.model is not None and q.model == self.failing_model:
            raise RuntimeError("tracking store unavailable")
        return await super().query(project_id, q)


class FakeHandoffs(Handoffs):
    def __init__(self, handoff: HandoffView | None = None) -> None:
        self._handoff = handoff

    def set_current(self, handoff: HandoffView | None) -> None:
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


async def preset_gate_cursor(databases: Databases, cursors: dict[str, datetime]) -> None:
    """Store the per-model team_gate cursor map, as if each model had been seeded then."""
    engine = await databases.project(PROJECT_ID)
    value = json.dumps({name: to_iso(at) for name, at in cursors.items()})
    async with engine.begin() as conn:
        await conn.execute(project_settings.insert().values(key=CURSOR_KEY, value=value))


async def stored_gate_cursor(databases: Databases) -> str:
    """The stored team_gate cursor value, unparsed."""
    engine = await databases.project(PROJECT_ID)
    async with engine.connect() as conn:
        raw = (
            await conn.execute(
                select(project_settings.c.value).where(project_settings.c.key == CURSOR_KEY)
            )
        ).scalar_one_or_none()
    assert raw is not None, f"{CURSOR_KEY} is not set"
    return str(raw)


def parse_gate_cursors(raw: str) -> dict[str, datetime]:
    assert raw.startswith("{"), f"{CURSOR_KEY} is not a per-model map: {raw!r}"
    parsed = cast(dict[str, str], json.loads(raw))
    return {name: datetime.fromisoformat(at) for name, at in parsed.items()}


async def gate_cursors(databases: Databases) -> dict[str, datetime]:
    """Read the per-model team_gate cursor map."""
    return parse_gate_cursors(await stored_gate_cursor(databases))


def gate_model(
    name: str, promotion: Literal["team_gate", "person"] = "team_gate"
) -> dict[str, Any]:
    return sample_model(
        name,
        promotion=promotion,
        environments={"production": "production"},
        production_signal={"kind": "tracker_alias", "ref": "production"},
    )


def add_candidate(
    tracking: FakeTracking,
    model: str,
    version: str,
    run_key: str | None,
) -> None:
    """Add a model version and, when it has a run key, its run, tagged as app-requested."""
    created = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    tracking.versions.append(
        ModelVersionRow(
            source_id="src_1",
            model=model,
            version=version,
            run_key=run_key,
            aliases=[],
            created=created,
            evaluation="present",
        )
    )
    if run_key is not None:
        tracking.runs.append(
            RunRow(
                source_id="src_1",
                key=run_key,
                experiment="exp_1",
                status="succeeded",
                start=created - timedelta(minutes=10),
                end=created,
                params={},
                tags={REQUEST_TAG: f"tr_{run_key}"},
                git_commit="sha123",
                datasets=[],
            )
        )


async def insert_person_promotion(databases: Databases, model: str, version: str) -> None:
    """A promotion a person already decided (mode person, through a proposal)."""
    engine = await databases.project(PROJECT_ID)
    async with engine.begin() as conn:
        await conn.execute(
            promotions_table.insert().values(
                id=f"prm_person_{model}_{version}",
                model=model,
                candidate_version=version,
                champion_version="v0",
                mode="person",
                decided_by="person",
                status="rejected",
                report_id=None,
                refusals="[]",
                created=to_iso(datetime(2026, 10, 31, 9, 0, tzinfo=UTC)),
                applied_at=None,
                proposal_id=f"prp_{model}_{version}",
            )
        )


class Gate:
    """One observer and its promotions service over shared fakes."""

    def __init__(
        self, databases: Databases, handoff: HandoffView, tracking: FakeTracking | None = None
    ) -> None:
        self.tracking = tracking or FakeTracking()
        self.handoffs = FakeHandoffs(handoff)
        self.reader = FakeEvaluationReader()
        self.notifier = RecordingNotifier()
        self.promotions = SqlitePromotions(
            databases=databases,
            tracking=self.tracking,
            handoffs=self.handoffs,
            reader=self.reader,
            reports=FakeReports(),
            hooks=cast(Hooks, FailingHooks()),
            events=SqliteEventLog(databases),
        )
        self.observer = TeamGateObserver(
            databases=databases,
            tracking=self.tracking,
            handoffs=self.handoffs,
            reader=self.reader,
            reports=FakeReports(),
            promotions=self.promotions,
            notifier=self.notifier,
            on_call=FakeOnCallPeople([make_on_call_person()]),
        )

    def decide(
        self,
        model: str,
        version: str,
        run_key: str,
        decision: Literal["promote", "hold", "reject"] | None = "promote",
    ) -> None:
        self.reader.views = [
            v
            for v in self.reader.views
            if not (v.record.model == model and v.record.version == version)
        ]
        self.reader.views.append(make_eval_view(model, version, run_key, decision=decision))


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

    # The team_gate model's cursor is seeded at t0
    assert await gate_cursors(databases) == {"fraud_detection": t0}
    assert len(notifier.calls) == 0


@pytest.mark.anyio
async def test_promote_decision_records_applied_and_notifies(databases: Databases) -> None:
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    model = "fraud_detection"
    candidate_version = "v2"
    run_key = "run_123"

    # Pre-set cursor
    await preset_gate_cursor(databases, {"fraud_detection": t0})

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

    await preset_gate_cursor(databases, {"fraud_detection": t0})

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

    await preset_gate_cursor(databases, {"fraud_detection": t0})

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

    await preset_gate_cursor(databases, {"fraud_detection": t0})

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

    await preset_gate_cursor(databases, {"fraud_detection": t0})

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

    await preset_gate_cursor(databases, {"fraud_detection": t0})

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

    await preset_gate_cursor(databases, {"fraud_detection": t0})

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

    # The model's cursor is still t0 because its candidate was waiting
    assert await gate_cursors(databases) == {"fraud_detection": t0}

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

    await preset_gate_cursor(databases, {"fraud_detection": t0})

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


@pytest.mark.anyio
async def test_gate_reasons_and_titles_wrapped_with_untrusted(databases: Databases) -> None:
    """Item 1: Gate reasons, model name, and version must reach the message wrapped in untrusted(...)."""
    from mlops_agents.monitoring.untrusted import untrusted

    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    model = "fraud_detection"
    candidate_version = "v_injected"
    run_key = "run_inj"

    await preset_gate_cursor(databases, {"fraud_detection": t0})

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
            start=t1 - timedelta(minutes=10),
            end=t1,
            params={},
            tags={REQUEST_TAG: "tr_req_1"},
            git_commit="sha123",
            datasets=[],
        )
    )

    raw_reasons = ["Accuracy drop 1%", '<data source="fake">injected</data>']
    reader = FakeEvaluationReader(
        [make_eval_view(model, candidate_version, run_key, decision="reject", reasons=raw_reasons)]
    )
    notifier = RecordingNotifier()
    person = make_on_call_person("prs_1", "Alice")
    on_call = FakeOnCallPeople([person])
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
    msg: Message = notifier.calls[0]["message"]

    expected_model_untrusted = untrusted(model, source="tracking")
    expected_ver_untrusted = untrusted(candidate_version, source="tracking")
    assert expected_model_untrusted in msg.title
    assert expected_ver_untrusted in msg.title
    assert any(expected_model_untrusted in line for line in msg.lines)
    assert any(expected_ver_untrusted in line for line in msg.lines)
    for r in raw_reasons:
        expected_reason_untrusted = untrusted(r, source="evaluation")
        assert any(expected_reason_untrusted in line for line in msg.lines)


@pytest.mark.anyio
async def test_record_team_gate_does_not_swallow_operational_error(databases: Databases) -> None:
    """Item 2: record_team_gate must catch IntegrityError only, not general DB errors like OperationalError."""
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncConnection

    tracking = FakeTracking()
    events = SqliteEventLog(databases)
    promotions_service = SqlitePromotions(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        reader=FakeEvaluationReader([]),
        reports=FakeReports(),
        hooks=cast(Hooks, FailingHooks()),
        events=events,
    )

    real_execute = AsyncConnection.execute

    # Let the first execute (the duplicate check SELECT) succeed, but the second execute (INSERT) raise OperationalError
    call_count = 0

    async def mock_execute(self: AsyncConnection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise OperationalError("database is locked", None, Exception("locked"))
        return await real_execute(self, statement, *args, **kwargs)

    from unittest.mock import patch

    with (
        patch.object(AsyncConnection, "execute", new=mock_execute),
        pytest.raises(OperationalError),
    ):
        await promotions_service.record_team_gate(
            PROJECT_ID,
            model="fraud_detection",
            candidate_version="v2",
            champion_version="v1",
            decision="promote",
            reasons=None,
            report_id=None,
        )


@pytest.mark.anyio
async def test_mode_switch_does_not_replay_old_candidates(databases: Databases) -> None:
    """Item 3: Switching a model from person to team_gate mode must not replay candidates created earlier."""
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    t2 = t0 + timedelta(minutes=10)
    model = "fraud_detection"

    # Start with person mode handoff
    person_handoff = make_handoff(
        models=[
            sample_model(
                model,
                promotion="person",
                environments={"production": "production"},
                production_signal={"kind": "tracker_alias", "ref": "production"},
            )
        ]
    )
    handoffs = FakeHandoffs(person_handoff)

    tracking = FakeTracking()
    # Candidate version v1 created during person mode at t1
    tracking.versions.append(
        ModelVersionRow(
            source_id="src_1",
            model=model,
            version="v1",
            run_key="run_1",
            aliases=[],
            created=t1,
            evaluation="present",
        )
    )
    tracking.runs.append(
        RunRow(
            source_id="src_1",
            key="run_1",
            experiment="exp_1",
            status="succeeded",
            start=t1 - timedelta(minutes=10),
            end=t1,
            params={},
            tags={REQUEST_TAG: "tr_req_1"},
            git_commit="sha123",
            datasets=[],
        )
    )

    reader = FakeEvaluationReader([make_eval_view(model, "v1", "run_1", decision="promote")])
    notifier = RecordingNotifier()
    person = make_on_call_person("prs_1", "Alice")
    on_call = FakeOnCallPeople([person])
    events = SqliteEventLog(databases)
    promotions_service = SqlitePromotions(
        databases=databases,
        tracking=tracking,
        handoffs=handoffs,
        reader=reader,
        reports=FakeReports(),
        hooks=cast(Hooks, FailingHooks()),
        events=events,
    )

    observer = TeamGateObserver(
        databases=databases,
        tracking=tracking,
        handoffs=handoffs,
        reader=reader,
        reports=FakeReports(),
        promotions=promotions_service,
        notifier=notifier,
        on_call=on_call,
    )

    # First sync seeds cursor at t0
    await observer.synced(PROJECT_ID, now=t0)
    # Sync at t1 + 1s while in person mode
    await observer.synced(PROJECT_ID, now=t1 + timedelta(seconds=1))

    # Switch handoff to team_gate mode at t2
    gate_handoff = make_handoff(
        models=[
            sample_model(
                model,
                promotion="team_gate",
                environments={"production": "production"},
                production_signal={"kind": "tracker_alias", "ref": "production"},
            )
        ]
    )
    handoffs.set_current(gate_handoff)

    # Sync at t2 + 1s
    await observer.synced(PROJECT_ID, now=t2 + timedelta(seconds=1))

    # Old candidate v1 must NOT be replayed in team_gate mode
    history = await promotions_service.history(PROJECT_ID, model=model)
    assert len(history) == 0
    assert len(notifier.calls) == 0


@pytest.mark.anyio
async def test_late_reported_version_with_older_created_at_is_processed(
    databases: Databases,
) -> None:
    """Item 4: A version the tracker reports late with an older registry created_at must not fall behind cursor."""
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=10)
    t2 = t0 + timedelta(minutes=20)
    model = "fraud_detection"

    tracking = FakeTracking()
    notifier = RecordingNotifier()
    person = make_on_call_person("prs_1", "Alice")
    on_call = FakeOnCallPeople([person])
    events = SqliteEventLog(databases)
    reader = FakeEvaluationReader([])
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

    # First sync at t0
    await observer.synced(PROJECT_ID, now=t0)

    # Candidate 1 created at t1
    tracking.versions.append(
        ModelVersionRow(
            source_id="src_1",
            model=model,
            version="v1",
            run_key="run_1",
            aliases=[],
            created=t1,
            evaluation="present",
        )
    )
    tracking.runs.append(
        RunRow(
            source_id="src_1",
            key="run_1",
            experiment="exp_1",
            status="succeeded",
            start=t1 - timedelta(minutes=5),
            end=t1,
            params={},
            tags={REQUEST_TAG: "tr_req_1"},
            git_commit="sha123",
            datasets=[],
        )
    )
    reader.views.append(make_eval_view(model, "v1", "run_1", decision="promote"))

    # Sync at t1 processes v1 and advances cursor to t1
    await observer.synced(PROJECT_ID, now=t1 + timedelta(seconds=1))
    history1 = await promotions_service.history(PROJECT_ID, model=model)
    assert len(history1) == 1

    # Now at t2, a late-reported version arrives whose registry created_at was t0 + 2m (< t1)
    late_created = t0 + timedelta(minutes=2)
    candidate_version = "v_late"
    run_key = "run_late"

    tracking.versions.append(
        ModelVersionRow(
            source_id="src_1",
            model=model,
            version=candidate_version,
            run_key=run_key,
            aliases=[],
            created=late_created,
            evaluation="present",
        )
    )
    tracking.runs.append(
        RunRow(
            source_id="src_1",
            key=run_key,
            experiment="exp_1",
            status="succeeded",
            start=late_created - timedelta(minutes=5),
            end=late_created,
            params={},
            tags={REQUEST_TAG: "tr_req_late"},
            git_commit="sha123",
            datasets=[],
        )
    )
    reader.views.append(make_eval_view(model, candidate_version, run_key, decision="promote"))

    # Sync at t2 sees it for the first time
    await observer.synced(PROJECT_ID, now=t2)

    # Must be processed despite created_at < cursor(t1)
    history2 = await promotions_service.history(PROJECT_ID, model=model)
    assert len(history2) == 2
    assert any(p.candidate_version == candidate_version for p in history2)


@pytest.mark.anyio
async def test_version_syncing_before_run_row_waits_and_is_not_dropped(
    databases: Databases,
) -> None:
    """Item 5: A version whose run row has not synced yet must wait and not be dropped by advancing the cursor."""
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    t2 = t0 + timedelta(minutes=10)
    model = "fraud_detection"
    candidate_version = "v_early_ver"
    run_key = "run_delayed"

    tracking = FakeTracking()
    notifier = RecordingNotifier()
    person = make_on_call_person("prs_1", "Alice")
    on_call = FakeOnCallPeople([person])
    events = SqliteEventLog(databases)
    reader = FakeEvaluationReader(
        [make_eval_view(model, candidate_version, run_key, decision="promote")]
    )
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

    # Seed cursor at t0
    await observer.synced(PROJECT_ID, now=t0)

    # At t1: version is synced into tracking, but run_row is NOT synced yet!
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

    await observer.synced(PROJECT_ID, now=t1)

    # Nothing should be recorded yet, and the model's cursor should be held (waiting)
    history = await promotions_service.history(PROJECT_ID, model=model)
    assert len(history) == 0
    assert len(notifier.calls) == 0
    assert await gate_cursors(databases) == {model: t0}

    # At t2: run_row is now synced into tracking with REQUEST_TAG
    tracking.runs.append(
        RunRow(
            source_id="src_1",
            key=run_key,
            experiment="exp_1",
            status="succeeded",
            start=t1 - timedelta(minutes=5),
            end=t1,
            params={},
            tags={REQUEST_TAG: "tr_req_early"},
            git_commit="sha123",
            datasets=[],
        )
    )

    await observer.synced(PROJECT_ID, now=t2)

    # Now it must be processed!
    history = await promotions_service.history(PROJECT_ID, model=model)
    assert len(history) == 1
    assert history[0].candidate_version == candidate_version
    assert len(notifier.calls) == 1


@pytest.mark.anyio
async def test_reasons_none_handled_through_observer(databases: Databases) -> None:
    """Test that reasons=None passes cleanly through the observer without failure."""
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    model = "fraud_detection"
    candidate_version = "v_noreasons"
    run_key = "run_noreasons"

    await preset_gate_cursor(databases, {"fraud_detection": t0})

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
            start=t1 - timedelta(minutes=10),
            end=t1,
            params={},
            tags={REQUEST_TAG: "tr_req_1"},
            git_commit="sha123",
            datasets=[],
        )
    )

    # reasons is explicitly None
    reader = FakeEvaluationReader(
        [make_eval_view(model, candidate_version, run_key, decision="hold", reasons=None)]
    )
    notifier = RecordingNotifier()
    person = make_on_call_person("prs_1", "Alice")
    on_call = FakeOnCallPeople([person])
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

    history = await promotions_service.history(PROJECT_ID, model=model)
    assert len(history) == 1
    assert history[0].status == "refused"
    assert history[0].refusals == []
    assert len(notifier.calls) == 1


@pytest.mark.anyio
async def test_person_decision_is_not_replayed_after_cursor_pin_and_switch(
    databases: Databases,
) -> None:
    """A waiting team_gate candidate must not let a person's decision be re-decided later."""
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    gate = Gate(databases, make_handoff([gate_model("model_a"), gate_model("model_b", "person")]))

    await gate.observer.synced(PROJECT_ID, now=t0)

    # A's requested candidate has no decision yet: it waits.
    add_candidate(gate.tracking, "model_a", "a1", "run_a1")
    gate.decide("model_a", "a1", "run_a1", decision=None)
    # B (person mode) gets v1, which a person already decided.
    add_candidate(gate.tracking, "model_b", "b1", "run_b1")
    gate.decide("model_b", "b1", "run_b1", decision="promote")
    await insert_person_promotion(databases, "model_b", "b1")

    await gate.observer.synced(PROJECT_ID, now=t0 + timedelta(minutes=5))

    gate.handoffs.set_current(make_handoff([gate_model("model_a"), gate_model("model_b")]))
    await gate.observer.synced(PROJECT_ID, now=t0 + timedelta(minutes=10))
    await gate.observer.synced(PROJECT_ID, now=t0 + timedelta(minutes=15))

    history = await gate.promotions.history(PROJECT_ID, model="model_b")
    assert [(p.candidate_version, p.mode) for p in history] == [("b1", "person")]
    assert gate.notifier.calls == []


@pytest.mark.anyio
async def test_already_decided_candidate_is_skipped_and_does_not_hold_cursor(
    databases: Databases,
) -> None:
    """A version that already has a promotion row in any mode is never a team_gate candidate."""
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    gate = Gate(databases, make_handoff([gate_model("model_b")]))

    await gate.observer.synced(PROJECT_ID, now=t0)

    # Seen after B's cursor, already decided by a person, and its evaluation has no decision yet.
    add_candidate(gate.tracking, "model_b", "b1", "run_b1")
    gate.decide("model_b", "b1", "run_b1", decision=None)
    await insert_person_promotion(databases, "model_b", "b1")

    await gate.observer.synced(PROJECT_ID, now=t1)
    cursor_after_first_look = await stored_gate_cursor(databases)

    # The team's gate later reports a decision for it: still not recorded.
    gate.decide("model_b", "b1", "run_b1", decision="promote")
    await gate.observer.synced(PROJECT_ID, now=t1 + timedelta(minutes=5))

    history = await gate.promotions.history(PROJECT_ID, model="model_b")
    assert [(p.candidate_version, p.mode) for p in history] == [("b1", "person")]
    assert gate.notifier.calls == []
    assert parse_gate_cursors(cursor_after_first_look)["model_b"] == t1


@pytest.mark.anyio
async def test_version_without_run_key_does_not_block_other_models(databases: Databases) -> None:
    """A version with no run key is not app-requested: skipped, it holds no model's cursor."""
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    t2 = t0 + timedelta(minutes=10)
    t3 = t0 + timedelta(minutes=15)
    gate = Gate(databases, make_handoff([gate_model("model_a"), gate_model("model_b", "person")]))

    await gate.observer.synced(PROJECT_ID, now=t0)

    # A's version has no run key; B, still in person mode, gets a requested, decided version.
    add_candidate(gate.tracking, "model_a", "a1", None)
    add_candidate(gate.tracking, "model_b", "b_old", "run_b_old")
    gate.decide("model_b", "b_old", "run_b_old", decision="promote")
    await gate.observer.synced(PROJECT_ID, now=t1)

    # B enters team_gate: its version from before entry is not a candidate.
    gate.handoffs.set_current(make_handoff([gate_model("model_a"), gate_model("model_b")]))
    await gate.observer.synced(PROJECT_ID, now=t2)

    assert await gate.promotions.history(PROJECT_ID, model="model_b") == []
    assert gate.notifier.calls == []

    # Versions arriving after B's entry are decided, A's included.
    add_candidate(gate.tracking, "model_b", "b_new", "run_b_new")
    gate.decide("model_b", "b_new", "run_b_new", decision="promote")
    add_candidate(gate.tracking, "model_a", "a2", "run_a2")
    gate.decide("model_a", "a2", "run_a2", decision="reject")
    await gate.observer.synced(PROJECT_ID, now=t3)

    history_b = await gate.promotions.history(PROJECT_ID, model="model_b")
    assert [(p.candidate_version, p.status) for p in history_b] == [("b_new", "applied")]
    history_a = await gate.promotions.history(PROJECT_ID, model="model_a")
    assert [(p.candidate_version, p.status) for p in history_a] == [("a2", "refused")]
    assert len(gate.notifier.calls) == 2
    assert await gate_cursors(databases) == {"model_a": t3, "model_b": t3}


@pytest.mark.anyio
async def test_model_reentering_team_gate_is_seeded_again(databases: Databases) -> None:
    """Versions that arrive while a model is in person mode are not decided when it re-enters team_gate."""
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    gate = Gate(databases, make_handoff([gate_model("model_a"), gate_model("model_pin")]))

    await gate.observer.synced(PROJECT_ID, now=t0)

    # Another team_gate model keeps a candidate waiting the whole time.
    add_candidate(gate.tracking, "model_pin", "p1", "run_p1")
    gate.decide("model_pin", "p1", "run_p1", decision=None)
    await gate.observer.synced(PROJECT_ID, now=t0 + timedelta(minutes=5))

    # A leaves team_gate and gets requested, decided versions while in person mode.
    gate.handoffs.set_current(
        make_handoff([gate_model("model_a", "person"), gate_model("model_pin")])
    )
    add_candidate(gate.tracking, "model_a", "a1", "run_a1")
    gate.decide("model_a", "a1", "run_a1", decision="promote")
    add_candidate(gate.tracking, "model_a", "a2", "run_a2")
    gate.decide("model_a", "a2", "run_a2", decision="reject")
    await gate.observer.synced(PROJECT_ID, now=t0 + timedelta(minutes=10))

    # A re-enters team_gate.
    gate.handoffs.set_current(make_handoff([gate_model("model_a"), gate_model("model_pin")]))
    await gate.observer.synced(PROJECT_ID, now=t0 + timedelta(minutes=15))

    assert await gate.promotions.history(PROJECT_ID, model="model_a") == []
    assert gate.notifier.calls == []

    # A version arriving after re-entry is decided.
    add_candidate(gate.tracking, "model_a", "a3", "run_a3")
    gate.decide("model_a", "a3", "run_a3", decision="promote")
    await gate.observer.synced(PROJECT_ID, now=t0 + timedelta(minutes=20))

    history = await gate.promotions.history(PROJECT_ID, model="model_a")
    assert [(p.candidate_version, p.status) for p in history] == [("a3", "applied")]
    assert len(gate.notifier.calls) == 1


@pytest.mark.anyio
async def test_model_and_version_names_containing_colons(databases: Databases) -> None:
    """Seen state and cursor are keyed by the exact model name, never parsed from a joined key."""
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    model = "team:model"
    gate = Gate(databases, make_handoff([gate_model(model)]))

    # Exists before the first sync: seeded, never a candidate.
    add_candidate(gate.tracking, model, "v:1", "run:1")
    gate.decide(model, "v:1", "run:1", decision="promote")

    await gate.observer.synced(PROJECT_ID, now=t0)
    await gate.observer.synced(PROJECT_ID, now=t0 + timedelta(minutes=5))

    assert await gate.promotions.history(PROJECT_ID, model=model) == []
    assert gate.notifier.calls == []

    add_candidate(gate.tracking, model, "v:2", "run:2")
    gate.decide(model, "v:2", "run:2", decision="promote")
    await gate.observer.synced(PROJECT_ID, now=t0 + timedelta(minutes=10))
    await gate.observer.synced(PROJECT_ID, now=t0 + timedelta(minutes=15))

    history = await gate.promotions.history(PROJECT_ID, model=model)
    assert [p.candidate_version for p in history] == ["v:2"]
    assert len(gate.notifier.calls) == 1
    assert set(await gate_cursors(databases)) == {model}


@pytest.mark.anyio
async def test_record_team_gate_refuses_a_version_a_person_decided(databases: Databases) -> None:
    """record_team_gate's own guard covers rows in any mode, not only team_gate rows."""
    gate = Gate(databases, make_handoff([gate_model("model_b")]))
    await insert_person_promotion(databases, "model_b", "b1")

    recorded = await gate.promotions.record_team_gate(
        PROJECT_ID,
        model="model_b",
        candidate_version="b1",
        champion_version="v0",
        decision="promote",
        reasons=None,
        report_id=None,
    )

    assert recorded is None
    history = await gate.promotions.history(PROJECT_ID, model="model_b")
    assert [(p.candidate_version, p.mode) for p in history] == [("b1", "person")]


@pytest.mark.anyio
async def test_seed_is_kept_when_a_later_model_fails_in_the_same_sync(databases: Databases) -> None:
    """A model's seed is durable on its own: a later failure in the sync cannot undo it."""
    t0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    tracking = ModelFailingTracking(failing_model="model_b")
    gate = Gate(
        databases, make_handoff([gate_model("model_a"), gate_model("model_b")]), tracking=tracking
    )

    # A is seeded, then reading B's versions fails.
    with pytest.raises(RuntimeError, match="tracking store unavailable"):
        await gate.observer.synced(PROJECT_ID, now=t0)

    # A requested, decided version arrives before the next sync, and tracking recovers.
    add_candidate(gate.tracking, "model_a", "a1", "run_a1")
    gate.decide("model_a", "a1", "run_a1", decision="promote")
    tracking.failing_model = None

    await gate.observer.synced(PROJECT_ID, now=t1)

    history = await gate.promotions.history(PROJECT_ID, model="model_a")
    assert [(p.candidate_version, p.status) for p in history] == [("a1", "applied")]
    assert len(gate.notifier.calls) == 1
