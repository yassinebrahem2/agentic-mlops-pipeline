"""Tests for fix outcomes, circuit breaker, and decline wake (Task 6, #491)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

import pytest
from sqlalchemy import insert

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.expectations.contract import (
    ExpectationKey,
    Expectations,
    ExpectationSpec,
    ExpectationState,
)
from mlops_agents.monitoring.hooks.contract import (
    HookCall,
    HookRefused,
    HookResult,
    HookView,
    PlaceholderResolver,
    RunContext,
)
from mlops_agents.monitoring.incidents.contract import (
    IncidentChange,
    Incidents,
    IncidentView,
    WakeLabel,
)
from mlops_agents.monitoring.untrusted import untrusted
from mlops_agents.notifications.contract import Message, NotificationKind, OnCallPerson
from mlops_agents.proposals.contract import Handoffs, HandoffView
from mlops_agents.proposals.payloads import CommandSpec, Contact, HandoffPayload, OnCallHours
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import fix_requests
from mlops_agents.tracking.contract import Page, RunRow, Tracking, TrackingQuery
from mlops_agents.training.constants import (
    REQUEST_TAG,
    RETRAINING_AGENT,
)
from mlops_agents.training.contract import TrainingWindow
from mlops_agents.training.fixes import SqliteFixes
from mlops_agents.training.lifecycle import TrainingLifecycle
from mlops_agents.training.linking import RequestLinker
from mlops_agents.training.outcomes import FixOutcomeObserver
from mlops_agents.training.requests import SqliteTraining
from tests.proposals.samples import handoff
from tests.providers.fake_clock import FakeClock


class FakeWaker:
    def __init__(self) -> None:
        self.wakes: list[dict[str, Any]] = []

    async def wake(
        self,
        project_id: str,
        agent: str,
        label: WakeLabel,
        subject_id: str,
        text: str,
        key: str,
    ) -> None:
        self.wakes.append(
            {
                "project_id": project_id,
                "agent": agent,
                "label": label,
                "subject_id": subject_id,
                "text": text,
                "key": key,
            }
        )


class FakeIncidents:
    def __init__(self) -> None:
        self._incidents: dict[str, IncidentView] = {}
        self.observers: list[Any] = []

    def add(self, inc: IncidentView) -> None:
        self._incidents[inc.id] = inc

    async def get(self, project_id: str, incident_id: str) -> IncidentView | None:
        return self._incidents.get(incident_id)

    async def holds(self, project_id: str, model: str) -> list[Any]:
        return []

    async def list(self, project_id: str, **kwargs: Any) -> Page[IncidentView]:
        return Page[IncidentView](rows=[], total=0, truncated=False)

    async def signals(self, project_id: str, incident_id: str) -> list[Any]:
        return []

    def add_observer(self, observer: Any) -> None:
        self.observers.append(observer)


class FakeHandoffs:
    async def current(self, project_id: str) -> HandoffView | None:
        payload_dict = handoff()
        payload_dict["models"][0]["name"] = "churn_predictor"
        payload_dict["models"][0]["profile"]["label_maturity_days"] = 7
        payload = HandoffPayload.model_validate(payload_dict)
        return HandoffView(
            proposal_id="prop_handoff",
            version=1,
            approved=datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC),
            payload=payload,
        )


class FakeNotifier:
    def __init__(self) -> None:
        self.notifications: list[dict[str, Any]] = []

    async def notify(
        self,
        project_id: str,
        person: OnCallPerson,
        message: Message,
        *,
        kind: NotificationKind,
        incident_id: str | None,
    ) -> Literal["sent", "retrying", "failed"]:
        self.notifications.append(
            {
                "project_id": project_id,
                "person": person,
                "message": message,
                "kind": kind,
                "incident_id": incident_id,
            }
        )
        return "sent"


class FakeOnCallPeople:
    def __init__(self, people: list[OnCallPerson] | None = None) -> None:
        self.people = list(people or [])

    async def list(self, project_id: str) -> list[OnCallPerson]:
        return list(self.people)

    async def replace(self, project_id: str, people: Any) -> list[OnCallPerson]:
        return []


class FakeResolver(PlaceholderResolver):
    async def resolve(
        self, hook: HookView, ctx: RunContext, *, project_id: str | None = None
    ) -> dict[str, str]:
        return {"run_name": f"{ctx.model}-run"}


class FakeExpectations:
    def __init__(self) -> None:
        self.registered: list[ExpectationSpec] = []
        self.retired: list[ExpectationKey] = []
        self._states: list[ExpectationState] = []

    async def register(self, project_id: str, spec: ExpectationSpec) -> None:
        self.registered.append(spec)

    async def retire(self, project_id: str, key: ExpectationKey) -> None:
        self.retired.append(key)

    async def note_seen(self, project_id: str, key: ExpectationKey, ran_at: datetime) -> None:
        pass

    async def states(self, project_id: str) -> list[ExpectationState]:
        return list(self._states)

    def set_silent(self, ref: str, model: str) -> None:
        self._states.append(
            ExpectationState(
                key=ExpectationKey(kind="training_request", ref=ref, model=model),
                category="operational",
                active=True,
                status="silent",
            )
        )


class FakeTracking:
    def __init__(self) -> None:
        self.runs: list[RunRow] = []

    async def query(self, project_id: str, q: TrackingQuery) -> Page[RunRow]:
        return Page[RunRow](rows=list(self.runs), total=len(self.runs), truncated=False)


def make_incident(
    incident_id: str = "inc_1",
    model: str = "churn_predictor",
    status: str = "open",
) -> IncidentView:
    now = datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC)
    return IncidentView(
        id=incident_id,
        category="model_performance",
        subtype="drift",
        severity="medium",
        status=status,  # type: ignore[arg-type]
        cause="Feature drift detected",
        cause_key=f"drift:{model}",
        model=model,
        signal_ids=["sig_1"],
        prs=[],
        issues=[],
        held=False,
        merged_into=None,
        started_at=now,
        detected_at=now,
    )


def make_on_call_person(person_id: str = "alice", escalation_order: int = 1) -> OnCallPerson:
    return OnCallPerson(
        id=person_id,
        name=person_id.capitalize(),
        contacts=[Contact(channel="email", address=f"{person_id}@example.com")],
        hours=OnCallHours(start="09:00", end="17:00", days=["mon", "tue", "wed", "thu", "fri"]),
        timezone="UTC",
        escalation_order=escalation_order,
    )


def make_hook(hook_id: str = "hook_1", model: str = "churn_predictor") -> HookView:
    return HookView(
        id=hook_id,
        name=f"Hook {hook_id}",
        model=model,
        spec=CommandSpec(
            kind="train",
            type="command",
            template="python train.py",
            workflow=None,
            inputs={},
            placeholders=[],
            deploys=False,
            entry_files=[],
        ),
        status="active",
        credential_set=False,
        source="proposal",
        created=datetime.now(UTC),
    )


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def fake_waker() -> FakeWaker:
    return FakeWaker()


@pytest.fixture
def fake_notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def fake_on_call() -> FakeOnCallPeople:
    return FakeOnCallPeople([make_on_call_person("alice", 1), make_on_call_person("bob", 2)])


@pytest.fixture
def fake_incidents() -> FakeIncidents:
    return FakeIncidents()


@pytest.fixture
def fixes_store(
    databases: Databases,
    fake_incidents: FakeIncidents,
    fake_waker: FakeWaker,
    fake_clock: FakeClock,
    fake_notifier: FakeNotifier,
    fake_on_call: FakeOnCallPeople,
) -> SqliteFixes:
    events = SqliteEventLog(databases)
    return SqliteFixes(
        databases=databases,
        incidents=cast(Incidents, fake_incidents),
        waker=fake_waker,
        events=events,
        clock=fake_clock,
        handoffs=cast(Handoffs, FakeHandoffs()),
        notifier=fake_notifier,
        on_call=fake_on_call,
    )


@pytest.fixture
def training_store(databases: Databases, fake_clock: FakeClock) -> SqliteTraining:
    events = SqliteEventLog(databases)
    return SqliteTraining(databases=databases, events=events, clock=fake_clock)


# -------------------------------------------------------------------------
# 1. Outcome transitions
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_request_outcome_not_cleared(
    databases: Databases,
    fake_clock: FakeClock,
    fake_waker: FakeWaker,
    fake_incidents: FakeIncidents,
    fixes_store: SqliteFixes,
    training_store: SqliteTraining,
) -> None:
    inc = make_incident("inc_1", "churn_predictor")
    fake_incidents.add(inc)

    fix = await fixes_store.request("prj_1", "inc_1", "diagnoser", "drift detected")
    assert fix.outcome == "pending"

    # Retraining chooses retrain
    await fixes_store.choose(
        "prj_1",
        fix.id,
        RETRAINING_AGENT,
        "retrain",
        "retrain on recent window",
        {"re_threshold": "not sufficient", "recalibrate": "drift is structural"},
    )

    events = SqliteEventLog(databases)
    expectations = FakeExpectations()
    lifecycle = TrainingLifecycle(
        training=training_store,
        fixes=fixes_store,
        resolver=FakeResolver(),
        clock=fake_clock,
        events=events,
        expectations=cast(Expectations, expectations),
    )

    hook = make_hook("hook_1", "churn_predictor")
    call = HookCall(
        hook_id="hook_1",
        reason="test training run",
        params={
            "window_start": "2026-10-01T00:00:00Z",
            "window_end": "2026-10-20T00:00:00Z",
        },
        fix_id=fix.id,
    )

    call_out = await lifecycle.before("prj_1", hook, call)
    req_id = call_out.params["request_id"]

    # Hook runner fails
    await lifecycle.after(
        "prj_1",
        hook,
        call_out,
        HookResult(ok=False, detail="OOM killed", request_id=req_id),
    )

    # trq is failed
    trq = await training_store.request("prj_1", req_id)
    assert trq is not None
    assert trq.status == "failed"

    # fix outcome is not_cleared
    updated_fix = await fixes_store.get("prj_1", fix.id)
    assert updated_fix is not None
    assert updated_fix.outcome == "not_cleared"


@pytest.mark.asyncio
async def test_silent_request_outcome_not_cleared(
    databases: Databases,
    fake_clock: FakeClock,
    fake_waker: FakeWaker,
    fake_incidents: FakeIncidents,
    fixes_store: SqliteFixes,
    training_store: SqliteTraining,
) -> None:
    inc = make_incident("inc_1", "churn_predictor")
    fake_incidents.add(inc)

    fix = await fixes_store.request("prj_1", "inc_1", "diagnoser", "drift detected")
    await fixes_store.choose(
        "prj_1",
        fix.id,
        RETRAINING_AGENT,
        "retrain",
        "retrain reason",
        {"re_threshold": "no", "recalibrate": "no"},
    )

    # Start a request linked to this fix
    await training_store.create_request(
        "prj_1",
        request_id="trq_1",
        model="churn_predictor",
        hook_id="hook_1",
        mode="train",
        status="started",
        fix_id=fix.id,
        window=TrainingWindow(
            start=datetime(2026, 10, 1, tzinfo=UTC),
            end=datetime(2026, 10, 20, tzinfo=UTC),
        ),
    )
    await fixes_store.mark_running("prj_1", fix.id)

    expectations = FakeExpectations()
    expectations.set_silent("trq_1", "churn_predictor")
    tracking = FakeTracking()
    events = SqliteEventLog(databases)

    linker = RequestLinker(
        training=training_store,
        fixes=fixes_store,
        tracking=cast(Tracking, tracking),
        expectations=cast(Expectations, expectations),
        events=events,
        waker=fake_waker,
    )

    await linker.synced("prj_1")

    # Request is marked silent
    req = await training_store.request("prj_1", "trq_1")
    assert req is not None
    assert req.status == "silent"

    # Fix outcome is not_cleared
    updated_fix = await fixes_store.get("prj_1", fix.id)
    assert updated_fix is not None
    assert updated_fix.outcome == "not_cleared"


@pytest.mark.asyncio
async def test_linked_run_resolved_cleared_and_reopened_not_cleared(
    databases: Databases,
    fake_clock: FakeClock,
    fake_waker: FakeWaker,
    fake_incidents: FakeIncidents,
    fixes_store: SqliteFixes,
    training_store: SqliteTraining,
) -> None:
    inc = make_incident("inc_1", "churn_predictor", status="open")
    fake_incidents.add(inc)

    fix = await fixes_store.request("prj_1", "inc_1", "diagnoser", "drift")
    await fixes_store.choose(
        "prj_1",
        fix.id,
        RETRAINING_AGENT,
        "retrain",
        "retrain why",
        {"re_threshold": "no", "recalibrate": "no"},
    )

    # Request starts, links
    await training_store.create_request(
        "prj_1",
        request_id="trq_2",
        model="churn_predictor",
        hook_id="hook_1",
        mode="train",
        status="started",
        fix_id=fix.id,
        window=TrainingWindow(
            start=datetime(2026, 10, 1, tzinfo=UTC),
            end=datetime(2026, 10, 20, tzinfo=UTC),
        ),
    )
    await fixes_store.mark_running("prj_1", fix.id)

    tracking = FakeTracking()
    tracking.runs.append(
        RunRow(
            source_id="src_mlflow",
            key="run_xyz",
            experiment="exp",
            status="succeeded",
            start=fake_clock.now(),
            end=fake_clock.now() + timedelta(minutes=10),
            params={},
            tags={REQUEST_TAG: "trq_2"},
            git_commit=None,
            datasets=[],
        )
    )
    events = SqliteEventLog(databases)
    expectations = FakeExpectations()
    linker = RequestLinker(
        training=training_store,
        fixes=fixes_store,
        tracking=cast(Tracking, tracking),
        expectations=cast(Expectations, expectations),
        events=events,
        waker=fake_waker,
    )
    await linker.synced("prj_1")

    # Fix is now status="done", outcome="pending"
    fix_done = await fixes_store.get("prj_1", fix.id)
    assert fix_done is not None
    assert fix_done.status == "done"
    assert fix_done.outcome == "pending"

    # Outcome observer
    observer = FixOutcomeObserver(fixes_store, cast(Incidents, fake_incidents))

    # 1. Incident resolved -> fix becomes cleared
    inc_resolved = make_incident("inc_1", "churn_predictor", status="resolved")
    fake_incidents.add(inc_resolved)
    await observer.changed(
        "prj_1",
        IncidentChange(incident_id="inc_1", kind="status"),
    )

    fix_cleared = await fixes_store.get("prj_1", fix.id)
    assert fix_cleared is not None
    assert fix_cleared.outcome == "cleared"

    # 2. Incident reopened -> fix becomes not_cleared
    inc_reopened = make_incident("inc_1", "churn_predictor", status="reopened")
    fake_incidents.add(inc_reopened)
    await observer.changed(
        "prj_1",
        IncidentChange(incident_id="inc_1", kind="reopened"),
    )

    fix_reopened = await fixes_store.get("prj_1", fix.id)
    assert fix_reopened is not None
    assert fix_reopened.outcome == "not_cleared"


@pytest.mark.asyncio
async def test_re_threshold_chosen_resolved_cleared_and_reopened_not_cleared(
    fake_clock: FakeClock,
    fake_incidents: FakeIncidents,
    fixes_store: SqliteFixes,
) -> None:
    inc = make_incident("inc_1", "churn_predictor", status="open")
    fake_incidents.add(inc)

    fix = await fixes_store.request("prj_1", "inc_1", "diagnoser", "threshold drift")
    # Retraining chooses re_threshold (no training run)
    await fixes_store.choose(
        "prj_1",
        fix.id,
        RETRAINING_AGENT,
        "re_threshold",
        "adjust threshold to 0.65",
        {"recalibrate": "unnecessary", "retrain": "model is good"},
    )

    fix_chosen = await fixes_store.get("prj_1", fix.id)
    assert fix_chosen is not None
    assert fix_chosen.status == "chosen"
    assert fix_chosen.choice == "re_threshold"
    assert fix_chosen.outcome == "pending"

    observer = FixOutcomeObserver(fixes_store, cast(Incidents, fake_incidents))

    # Resolve -> outcome="cleared"
    inc_resolved = make_incident("inc_1", "churn_predictor", status="resolved")
    fake_incidents.add(inc_resolved)
    await observer.changed(
        "prj_1",
        IncidentChange(incident_id="inc_1", kind="status"),
    )

    fix_cleared = await fixes_store.get("prj_1", fix.id)
    assert fix_cleared is not None
    assert fix_cleared.outcome == "cleared"

    # Reopen -> outcome="not_cleared"
    inc_reopened = make_incident("inc_1", "churn_predictor", status="reopened")
    fake_incidents.add(inc_reopened)
    await observer.changed(
        "prj_1",
        IncidentChange(incident_id="inc_1", kind="reopened"),
    )

    fix_not_cleared = await fixes_store.get("prj_1", fix.id)
    assert fix_not_cleared is not None
    assert fix_not_cleared.outcome == "not_cleared"


@pytest.mark.asyncio
async def test_declined_and_escalated_never_count(
    fake_clock: FakeClock,
    fake_incidents: FakeIncidents,
    fixes_store: SqliteFixes,
    databases: Databases,
) -> None:
    inc1 = make_incident("inc_1", "churn_predictor", status="open")
    fake_incidents.add(inc1)

    # Declined fix
    fix1 = await fixes_store.request("prj_1", "inc_1", "diagnoser", "test evidence")
    await fixes_store.choose(
        "prj_1",
        fix1.id,
        RETRAINING_AGENT,
        "decline",
        "decline reason",
        {"re_threshold": "no", "recalibrate": "no", "retrain": "no"},
    )
    f1_view = await fixes_store.get("prj_1", fix1.id)
    assert f1_view is not None
    assert f1_view.status == "declined"
    assert f1_view.outcome == "pending"

    # Escalated fix (manually created or via request when circuit open)
    now_iso = fake_clock.now().isoformat()
    async with (await databases.project("prj_1")).begin() as conn:
        await conn.execute(
            insert(fix_requests).values(
                id="fix_esc_1",
                incident_id="inc_1",
                model="churn_predictor",
                requested_by="diagnoser",
                evidence="escalated evidence",
                choice=None,
                why=None,
                why_not="{}",
                status="escalated",
                outcome="pending",
                created=now_iso,
            )
        )

    observer = FixOutcomeObserver(fixes_store, cast(Incidents, fake_incidents))

    # Resolve incident
    inc_resolved = make_incident("inc_1", "churn_predictor", status="resolved")
    fake_incidents.add(inc_resolved)
    await observer.changed(
        "prj_1",
        IncidentChange(incident_id="inc_1", kind="status"),
    )

    # Both remain pending
    f1 = await fixes_store.get("prj_1", fix1.id)
    f2 = await fixes_store.get("prj_1", "fix_esc_1")
    assert f1 is not None and f1.outcome == "pending"
    assert f2 is not None and f2.outcome == "pending"

    # Reopen incident
    inc_reopened = make_incident("inc_1", "churn_predictor", status="reopened")
    fake_incidents.add(inc_reopened)
    await observer.changed(
        "prj_1",
        IncidentChange(incident_id="inc_1", kind="reopened"),
    )

    f1 = await fixes_store.get("prj_1", fix1.id)
    f2 = await fixes_store.get("prj_1", "fix_esc_1")
    assert f1 is not None and f1.outcome == "pending"
    assert f2 is not None and f2.outcome == "pending"

    # Circuit breaker check: neither counts
    circuit_state = await fixes_store.circuit("prj_1", "churn_predictor")
    assert circuit_state.failed_fixes == 0
    assert not circuit_state.open


# -------------------------------------------------------------------------
# 2. Circuit breaker rules
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_opens_at_exact_n_within_window(
    databases: Databases,
    fake_clock: FakeClock,
    fake_incidents: FakeIncidents,
    fixes_store: SqliteFixes,
) -> None:
    inc = make_incident("inc_1", "churn_predictor")
    fake_incidents.add(inc)

    # circuit_breaker_fixes is 3 by default, circuit_breaker_window_days is 30
    now = fake_clock.now()

    # Add 2 not_cleared fixes
    for i in range(2):
        async with (await databases.project("prj_1")).begin() as conn:
            await conn.execute(
                insert(fix_requests).values(
                    id=f"fix_nc_{i}",
                    incident_id="inc_1",
                    model="churn_predictor",
                    requested_by="diagnoser",
                    evidence="ev",
                    choice="retrain",
                    why="why",
                    why_not="{}",
                    status="done",
                    outcome="not_cleared",
                    created=(now - timedelta(days=i + 1)).isoformat(),
                )
            )

    state = await fixes_store.circuit("prj_1", "churn_predictor")
    assert state.failed_fixes == 2
    assert not state.open

    # Add 3rd not_cleared fix
    async with (await databases.project("prj_1")).begin() as conn:
        await conn.execute(
            insert(fix_requests).values(
                id="fix_nc_2",
                incident_id="inc_1",
                model="churn_predictor",
                requested_by="diagnoser",
                evidence="ev",
                choice="retrain",
                why="why",
                why_not="{}",
                status="done",
                outcome="not_cleared",
                created=(now - timedelta(days=3)).isoformat(),
            )
        )

    state2 = await fixes_store.circuit("prj_1", "churn_predictor")
    assert state2.failed_fixes == 3
    assert state2.open


@pytest.mark.asyncio
async def test_circuit_ignores_older_fix_outside_window(
    databases: Databases,
    fake_clock: FakeClock,
    fake_incidents: FakeIncidents,
    fixes_store: SqliteFixes,
) -> None:
    inc = make_incident("inc_1", "churn_predictor")
    fake_incidents.add(inc)

    now = fake_clock.now()

    # 1 fix created 35 days ago (outside 30-day window)
    async with (await databases.project("prj_1")).begin() as conn:
        await conn.execute(
            insert(fix_requests).values(
                id="fix_old",
                incident_id="inc_1",
                model="churn_predictor",
                requested_by="diagnoser",
                evidence="ev",
                choice="retrain",
                why="why",
                why_not="{}",
                status="done",
                outcome="not_cleared",
                created=(now - timedelta(days=35)).isoformat(),
            )
        )

    # 2 fixes within window
    for i in range(2):
        async with (await databases.project("prj_1")).begin() as conn:
            await conn.execute(
                insert(fix_requests).values(
                    id=f"fix_recent_{i}",
                    incident_id="inc_1",
                    model="churn_predictor",
                    requested_by="diagnoser",
                    evidence="ev",
                    choice="retrain",
                    why="why",
                    why_not="{}",
                    status="done",
                    outcome="not_cleared",
                    created=(now - timedelta(days=i + 1)).isoformat(),
                )
            )

    state = await fixes_store.circuit("prj_1", "churn_predictor")
    # Total not_cleared is 3, but only 2 within window
    assert state.failed_fixes == 2
    assert not state.open


@pytest.mark.asyncio
async def test_open_circuit_escalates_skips_wake_notifies_and_refuses_run(
    databases: Databases,
    fake_clock: FakeClock,
    fake_waker: FakeWaker,
    fake_notifier: FakeNotifier,
    fake_on_call: FakeOnCallPeople,
    fake_incidents: FakeIncidents,
    fixes_store: SqliteFixes,
    training_store: SqliteTraining,
) -> None:
    inc = make_incident("inc_open", "churn_predictor")
    fake_incidents.add(inc)

    now = fake_clock.now()

    # Create 3 not_cleared fixes to open the circuit
    for i in range(3):
        async with (await databases.project("prj_1")).begin() as conn:
            await conn.execute(
                insert(fix_requests).values(
                    id=f"fix_nc_{i}",
                    incident_id="inc_open",
                    model="churn_predictor",
                    requested_by="diagnoser",
                    evidence="evidence",
                    choice="retrain",
                    why=f"why {i}",
                    why_not="{}",
                    status="done",
                    outcome="not_cleared",
                    created=(now - timedelta(days=i + 1)).isoformat(),
                )
            )

    # Verify circuit is open
    circuit_state = await fixes_store.circuit("prj_1", "churn_predictor")
    assert circuit_state.open

    # Request a fix while circuit is open
    fix_view = await fixes_store.request("prj_1", "inc_open", "diagnoser", "new spike")
    assert fix_view.status == "escalated"
    assert fix_view.outcome == "pending"

    # Verify Retraining is NOT woken
    retraining_wakes = [w for w in fake_waker.wakes if w["agent"] == RETRAINING_AGENT]
    assert len(retraining_wakes) == 0

    # Verify notification sent to first on-call person (alice)
    assert len(fake_notifier.notifications) == 1
    notif = fake_notifier.notifications[0]
    assert notif["kind"] == "escalation"
    assert notif["incident_id"] == "inc_open"
    assert notif["person"].id == "alice"
    assert notif["message"].urgent is False
    assert "Fix escalated" in notif["message"].title

    # Verify guards refuse training run with circuit_open
    window = TrainingWindow(start=now - timedelta(days=7), end=now)
    guard_res = await fixes_store.guards("prj_1", "churn_predictor", window)
    assert not guard_res.ok
    refusal_codes = [r.code for r in guard_res.refusals]
    assert "circuit_open" in refusal_codes

    # Verify lifecycle.before raises HookRefused
    events = SqliteEventLog(databases)
    lifecycle = TrainingLifecycle(
        training=training_store,
        fixes=fixes_store,
        resolver=FakeResolver(),
        clock=fake_clock,
        events=events,
    )
    hook = make_hook("hook_1", "churn_predictor")
    call = HookCall(
        hook_id="hook_1",
        reason="test training run",
        params={
            "window_start": (now - timedelta(days=7)).isoformat(),
            "window_end": now.isoformat(),
        },
        fix_id=fix_view.id,
    )
    with pytest.raises(HookRefused, match="circuit_open"):
        await lifecycle.before("prj_1", hook, call)


# -------------------------------------------------------------------------
# 3. Decline wake
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decline_wakes_requester_once_leaves_incident_open(
    fake_clock: FakeClock,
    fake_waker: FakeWaker,
    fake_incidents: FakeIncidents,
    fixes_store: SqliteFixes,
) -> None:
    inc = make_incident("inc_1", "churn_predictor", status="open")
    fake_incidents.add(inc)

    fix = await fixes_store.request("prj_1", "inc_1", "diagnoser_agent", "drift evidence")
    assert fix.status == "requested"

    # Reset wakes from the request
    fake_waker.wakes.clear()

    # Retraining declines the fix
    declined_view = await fixes_store.choose(
        "prj_1",
        fix.id,
        RETRAINING_AGENT,
        "decline",
        "Expected seasonal variation, no retrain needed.",
        {
            "re_threshold": "metrics still within limits",
            "recalibrate": "calibration is fine",
            "retrain": "seasonal drift expected",
        },
    )

    assert declined_view.status == "declined"
    assert declined_view.outcome == "pending"

    # Check requester woken
    assert len(fake_waker.wakes) == 1
    wake = fake_waker.wakes[0]
    assert wake["agent"] == "diagnoser_agent"
    assert wake["label"] == "incident"
    assert wake["subject_id"] == "inc_1"
    assert wake["key"] == fix.id
    assert (
        untrusted("Expected seasonal variation, no retrain needed.", source="fix_choice")
        in wake["text"]
    )

    # Incident remains open
    incident_after = await fake_incidents.get("prj_1", "inc_1")
    assert incident_after is not None
    assert incident_after.status == "open"
