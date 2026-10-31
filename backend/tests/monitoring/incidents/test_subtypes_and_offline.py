"""Tests for subtype determination at open, diagnoser waking, silence resolution, and resolved offline."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mlops_agents.errors import DomainError
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.expectations.contract import (
    ExpectationKey,
    ExpectationSpec,
    ExpectationState,
)
from mlops_agents.monitoring.incidents.contract import (
    IncidentPatch,
    SignalDraft,
)
from mlops_agents.monitoring.incidents.dispatch import DiagnosisDispatch
from mlops_agents.monitoring.incidents.from_results import SignalsFromResults
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.monitoring.waker import Waker
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import HandoffPayload
from mlops_agents.storage.databases import Databases
from tests.api.fake_sessions import FakeSessions
from tests.monitoring.incidents.conftest import (
    FakeClock,
    FakeMaintenance,
    FakeResults,
    FakeSeverityPolicy,
    make_stored_result,
)
from tests.proposals.samples import ci_dispatch, model
from tests.proposals.samples import handoff as sample_handoff_dict


class FakeHandoffs:
    def __init__(self, payload: HandoffPayload | None = None) -> None:
        self._payload = payload

    def add_observer(self, observer: object) -> None:
        pass

    async def current(self, project_id: str) -> HandoffView | None:
        if self._payload is None:
            return None
        return HandoffView(
            proposal_id="prop_test",
            version=1,
            approved=datetime.now(UTC),
            payload=self._payload,
        )

    async def store(self, project_id: str, proposal_id: str, payload: HandoffPayload) -> int:
        self._payload = payload
        return 1


class FakeExpectations:
    def __init__(self, states_list: list[ExpectationState] | None = None) -> None:
        self._states = states_list or []

    def set_states(self, states_list: list[ExpectationState]) -> None:
        self._states = states_list

    async def register(self, project_id: str, spec: ExpectationSpec) -> None:
        pass

    async def retire(self, project_id: str, key: ExpectationKey) -> None:
        pass

    async def note_seen(self, project_id: str, key: ExpectationKey, ran_at: datetime) -> None:
        pass

    async def activate_for_pr(self, project_id: str, pr_number: int) -> list[ExpectationKey]:
        return []

    async def evaluate(self, project_id: str, now: datetime) -> list[str]:
        return []

    async def states(self, project_id: str) -> list[ExpectationState]:
        return self._states


@pytest.mark.asyncio
async def test_subtypes_from_handoff_and_matching_diagnoser_woken(
    databases: Databases,
    event_log: SqliteEventLog,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    """Each subtype rule from a handoff fixture is determined at open and wakes the matching diagnoser."""
    # Build payload with train, deploy commands and production_signal
    data = sample_handoff_dict()
    m_dict = model(
        "churn",
        commands=[
            ci_dispatch(kind="train", workflow="train_model.yml"),
            ci_dispatch(kind="deploy", workflow="deploy_model.yml"),
        ],
        production_signal={"kind": "ci_job", "ref": "score_model.yml"},
    )
    data["models"] = [m_dict]
    payload = HandoffPayload.model_validate(data)

    handoffs = FakeHandoffs(payload)
    results = FakeResults()
    severity = FakeSeverityPolicy("medium")
    service = IncidentsService(
        databases=databases,
        events=event_log,
        results=results,
        severity=severity,
        maintenance=FakeMaintenance(),
        clock=fake_clock,
        handoffs=handoffs,
    )

    fake_sessions = FakeSessions()
    waker = Waker(databases, event_log, fake_clock)
    waker.bind(fake_sessions)
    dispatch = DiagnosisDispatch(service, waker)
    service.add_observer(dispatch)

    t0 = datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC)

    # 1. train command -> failed_training -> wakes retraining
    d_train = SignalDraft(
        kind="fired_result",
        category="operational",
        subject="train_model.yml",
        model="churn",
        ran_at=t0,
        detail={},
        source_ref="ci:train_model.yml",
    )
    out_train = await service.raise_signal(project_id, d_train, seen_at=t0)
    view_train = await service.get(project_id, out_train.incident_id)
    assert view_train is not None
    assert view_train.subtype == "failed_training"
    assert len(fake_sessions.posted_messages) == 1
    assert fake_sessions.posted_messages[-1][1] == "retraining"

    # 2. deploy command -> bad_deploy -> wakes deployment
    d_deploy = SignalDraft(
        kind="fired_result",
        category="operational",
        subject="deploy_model.yml",
        model="churn",
        ran_at=t0 + timedelta(minutes=5),
        detail={},
        source_ref="ci:deploy_model.yml",
    )
    out_deploy = await service.raise_signal(project_id, d_deploy, seen_at=t0 + timedelta(minutes=5))
    view_deploy = await service.get(project_id, out_deploy.incident_id)
    assert view_deploy is not None
    assert view_deploy.subtype == "bad_deploy"
    assert len(fake_sessions.posted_messages) == 2
    assert fake_sessions.posted_messages[-1][1] == "deployment"

    # 3. production signal ci_job -> failed_scoring -> wakes deployment
    d_score = SignalDraft(
        kind="fired_result",
        category="operational",
        subject="score_model.yml",
        model="churn",
        ran_at=t0 + timedelta(minutes=10),
        detail={},
        source_ref="ci:score_model.yml",
    )
    out_score = await service.raise_signal(project_id, d_score, seen_at=t0 + timedelta(minutes=10))
    view_score = await service.get(project_id, out_score.incident_id)
    assert view_score is not None
    assert view_score.subtype == "failed_scoring"
    assert len(fake_sessions.posted_messages) == 3
    assert fake_sessions.posted_messages[-1][1] == "deployment"

    # 4. silence draft -> subtype is silence
    d_silence = SignalDraft(
        kind="silence",
        category="data_quality",
        subject="orders",
        model=None,
        ran_at=t0 + timedelta(minutes=15),
        detail={},
    )
    out_silence = await service.raise_signal(
        project_id, d_silence, seen_at=t0 + timedelta(minutes=15)
    )
    view_silence = await service.get(project_id, out_silence.incident_id)
    assert view_silence is not None
    assert view_silence.subtype == "silence"


@pytest.mark.asyncio
async def test_silence_incident_resolved_lifecycle_with_expectations(
    databases: Databases,
    event_log: SqliteEventLog,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    """A silence incident cannot be resolved while expectation is silent, and can once ok."""
    results = FakeResults()
    severity = FakeSeverityPolicy("medium")
    handoffs = FakeHandoffs()
    service = IncidentsService(
        databases=databases,
        events=event_log,
        results=results,
        severity=severity,
        maintenance=FakeMaintenance(),
        clock=fake_clock,
        handoffs=handoffs,
    )

    exp_key = ExpectationKey(kind="feed", ref="raw_orders", model=None)
    fake_expectations = FakeExpectations(
        [
            ExpectationState(
                key=exp_key,
                category="data_quality",
                active=True,
                status="silent",
            )
        ]
    )
    service.bind_expectations(fake_expectations)

    t0 = datetime(2026, 10, 30, 8, 0, 0, tzinfo=UTC)
    draft = SignalDraft(
        kind="silence",
        category="data_quality",
        subject="raw_orders",
        model=None,
        ran_at=t0,
        detail={},
        expectation=exp_key,
    )
    outcome = await service.raise_signal(project_id, draft, seen_at=t0)
    inc_id = outcome.incident_id

    # 1. Attempt to resolve while expectation is silent -> refused with 409
    with pytest.raises(DomainError) as exc_info:
        await service.update(
            project_id,
            inc_id,
            agent="user",
            patch=IncidentPatch(status="resolved"),
        )
    assert exc_info.value.status == 409
    assert exc_info.value.code == "incident_still_firing"

    # 2. Expectation becomes ok -> resolving succeeds
    fake_expectations.set_states(
        [
            ExpectationState(
                key=exp_key,
                category="data_quality",
                active=True,
                status="ok",
            )
        ]
    )
    updated = await service.update(
        project_id,
        inc_id,
        agent="user",
        patch=IncidentPatch(status="resolved"),
    )
    assert updated.status == "resolved"
    assert updated.resolved_at is not None


@pytest.mark.asyncio
async def test_resolved_while_offline_catch_up_batch(
    databases: Databases,
    event_log: SqliteEventLog,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    """A catch-up batch (fired at 02:00, cleared at 03:00) opens one resolved-offline incident and wakes no one."""
    results = FakeResults()
    severity = FakeSeverityPolicy("medium")
    handoffs = FakeHandoffs()
    service = IncidentsService(
        databases=databases,
        events=event_log,
        results=results,
        severity=severity,
        maintenance=FakeMaintenance(),
        clock=fake_clock,
        handoffs=handoffs,
    )

    fake_sessions = FakeSessions()
    waker = Waker(databases, event_log, fake_clock)
    waker.bind(fake_sessions)
    dispatch = DiagnosisDispatch(service, waker)
    service.add_observer(dispatch)

    signals_from_results = SignalsFromResults(
        incidents=service,
        maintenance=FakeMaintenance(),
        results=results,
    )

    t_0200 = datetime(2026, 10, 30, 2, 0, 0, tzinfo=UTC)
    t_0300 = datetime(2026, 10, 30, 3, 0, 0, tzinfo=UTC)

    r_fired = make_stored_result(
        "res_fired_0200",
        check_id="chk_offline",
        subject="orders",
        model="churn",
        fired=True,
        ran_at=t_0200,
        seen_at=t_0300,
    )
    r_cleared = make_stored_result(
        "res_cleared_0300",
        check_id="chk_offline",
        subject="orders",
        model="churn",
        fired=False,
        ran_at=t_0300,
        seen_at=t_0300,
    )

    # Ingest catch-up batch into FakeResults
    results.latest_results = [r_cleared]

    await signals_from_results.stored(project_id, [r_fired, r_cleared])

    # 1. Incident was opened already resolved
    incidents_page = await service.list(project_id)
    assert len(incidents_page.rows) == 1
    inc = incidents_page.rows[0]
    assert inc.status == "resolved"
    assert inc.resolved_offline is True
    assert inc.resolved_at is not None

    # 2. Dispatch skipped it -> woke NO ONE!
    assert len(fake_sessions.posted_messages) == 0

    # 3. Now send a still-firing result at 04:00
    t_0400 = datetime(2026, 10, 30, 4, 0, 0, tzinfo=UTC)
    r_still_firing = make_stored_result(
        "res_firing_0400",
        check_id="chk_still_firing",
        subject="payments",
        model="churn",
        fired=True,
        ran_at=t_0400,
        seen_at=t_0400,
    )
    results.latest_results.append(r_still_firing)
    await signals_from_results.stored(project_id, [r_still_firing])

    incidents_page2 = await service.list(project_id)
    assert len(incidents_page2.rows) == 2
    inc2 = next(i for i in incidents_page2.rows if i.id != inc.id)
    assert inc2.status == "open"
    assert inc2.resolved_offline is False

    # Diagnoser was woken for the still-firing one!
    assert len(fake_sessions.posted_messages) == 1
    assert fake_sessions.posted_messages[0][1] == "data-quality"
