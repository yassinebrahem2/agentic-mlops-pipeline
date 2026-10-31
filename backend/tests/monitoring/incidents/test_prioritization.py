"""Tests for Alert Prioritization's run and AdjustSeverity (Task 6)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import RiskClass, ToolContext
from mlops_agents.agents.tools.names import ADJUST_SEVERITY
from mlops_agents.errors import DomainError
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.incidents.contract import (
    IncidentChange,
    SignalDraft,
)
from mlops_agents.monitoring.incidents.prioritization import (
    ALERT_PRIORITIZATION_AGENT,
    PrioritizationWake,
)
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.monitoring.incidents.tools import (
    AdjustSeverity,
    AdjustSeverityArgs,
)
from mlops_agents.monitoring.waker import WAKER_AUTHOR, Waker
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.gate_helpers import FakeFileAccess
from tests.agents.runtime.session_env import build_session_env
from tests.api.fake_sessions import FakeSessions
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.monitoring.incidents.conftest import (
    FakeClock,
    FakeMaintenance,
    FakeResults,
    FakeSeverityPolicy,
)


def _make_ctx(project_id: str, agent: str = ALERT_PRIORITIZATION_AGENT) -> ToolContext:
    return ToolContext(
        project_id=project_id,
        agent=agent,
        session_id="ses_1",
        turn_id="trn_1",
        files=FakeFileAccess(),
        task=None,
        tool_call_id="call_test_1",
    )


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def fake_results() -> FakeResults:
    return FakeResults()


@pytest.fixture
def fake_severity() -> FakeSeverityPolicy:
    return FakeSeverityPolicy(default_severity="medium")


@pytest.fixture
def fake_maintenance() -> FakeMaintenance:
    return FakeMaintenance()


@pytest.fixture
def event_log(databases: Databases) -> SqliteEventLog:
    return SqliteEventLog(databases)


@pytest.fixture
def fake_sessions() -> FakeSessions:
    return FakeSessions(no_chat_agents={ALERT_PRIORITIZATION_AGENT})


@pytest.fixture
def waker(
    databases: Databases,
    event_log: SqliteEventLog,
    fake_clock: FakeClock,
    fake_sessions: FakeSessions,
) -> Waker:
    w = Waker(databases, event_log, fake_clock)
    w.bind(fake_sessions)
    return w


@pytest.fixture
def incidents_service(
    databases: Databases,
    event_log: SqliteEventLog,
    fake_results: FakeResults,
    fake_severity: FakeSeverityPolicy,
    fake_maintenance: FakeMaintenance,
    fake_clock: FakeClock,
) -> IncidentsService:
    return IncidentsService(
        databases=databases,
        events=event_log,
        results=fake_results,
        severity=fake_severity,
        maintenance=fake_maintenance,
        clock=fake_clock,
    )


async def test_system_wake_reaches_chat_false_agent_and_user_refused(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    fake_clock: FakeClock,
    event_log: SqliteEventLog,
) -> None:
    """System wake reaches a chat: false agent and a user message to it is still refused."""
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
    )
    manager = env.manager
    project_id = env.project_id

    # 1. User message via send is refused with 409 agent_cannot_chat
    with pytest.raises(DomainError) as exc_send:
        await manager.send(project_id, ALERT_PRIORITIZATION_AGENT, "Hello")
    assert exc_send.value.status == 409
    assert exc_send.value.code == "agent_cannot_chat"

    # 2. Non-system post is also refused with 409 agent_cannot_chat
    with pytest.raises(DomainError) as exc_post:
        await manager.post(
            project_id,
            ALERT_PRIORITIZATION_AGENT,
            author_name="User",
            from_agent="user",
            text="Hello agent",
            exchange_id=None,
            hop=None,
            system=False,
        )
    assert exc_post.value.status == 409
    assert exc_post.value.code == "agent_cannot_chat"

    # 3. System post succeeds
    sys_msg = await manager.post(
        project_id,
        ALERT_PRIORITIZATION_AGENT,
        author_name="system",
        from_agent="system",
        text="Wakeup alert-prioritization",
        exchange_id=None,
        hop=None,
        system=True,
    )
    assert sys_msg.parts[0].type == "text"
    assert sys_msg.parts[0].text == "Wakeup alert-prioritization"

    # 4. Waker.wake reaches the agent without raising DomainError
    real_waker = Waker(databases, event_log, fake_clock)
    real_waker.bind(manager)
    await real_waker.wake(
        project_id,
        agent=ALERT_PRIORITIZATION_AGENT,
        label="incident",
        subject_id="inc_test_1",
        text="Incident inc_test_1 requires alert prioritization.",
        key="opened:inc_test_1",
    )

    await manager.close()


async def test_one_wake_per_change_and_none_after_own_adjustment(
    waker: Waker,
    fake_sessions: FakeSessions,
    event_log: SqliteEventLog,
    incidents_service: IncidentsService,
    fake_severity: FakeSeverityPolicy,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    """One wake per change and none after its own adjustment."""
    prioritization_wake = PrioritizationWake(waker=waker, events=event_log)
    incidents_service.add_observer(prioritization_wake)

    # Open an incident
    now = fake_clock.now()
    outcome = await incidents_service.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            result_id="res_1",
            subject="pipeline_test",
            category="data_quality",
            model="churn",
            ran_at=now,
            detail={"issue": "test"},
        ),
        seen_at=now,
    )
    inc_id = outcome.incident_id
    assert inc_id is not None

    # Incident opened triggered one wake
    assert len(fake_sessions.posted_messages) == 1
    p_id, agent, from_agent, text = fake_sessions.posted_messages[0]
    assert p_id == project_id
    assert agent == ALERT_PRIORITIZATION_AGENT
    assert from_agent == WAKER_AUTHOR
    assert "opened" in text

    # Duplicate opened change emits no second wake (deduplicated by key)
    await prioritization_wake.changed(
        project_id,
        IncidentChange(incident_id=inc_id, kind="opened"),
    )
    assert len(fake_sessions.posted_messages) == 1

    # Reopened triggers wake
    await prioritization_wake.changed(
        project_id,
        IncidentChange(incident_id=inc_id, kind="reopened"),
    )
    assert len(fake_sessions.posted_messages) == 2

    # Merged triggers wake
    await prioritization_wake.changed(
        project_id,
        IncidentChange(incident_id=inc_id, kind="merged"),
    )
    assert len(fake_sessions.posted_messages) == 3

    # Split triggers wake
    await prioritization_wake.changed(
        project_id,
        IncidentChange(incident_id=inc_id, kind="split"),
    )
    assert len(fake_sessions.posted_messages) == 4

    # Unrelated change (status, cause, hold) does NOT trigger wake
    await prioritization_wake.changed(
        project_id,
        IncidentChange(incident_id=inc_id, kind="status"),
    )
    await prioritization_wake.changed(
        project_id,
        IncidentChange(incident_id=inc_id, kind="cause"),
    )
    await prioritization_wake.changed(
        project_id,
        IncidentChange(incident_id=inc_id, kind="hold"),
    )
    assert len(fake_sessions.posted_messages) == 4

    # Now Alert Prioritization adjusts severity using AdjustSeverity tool
    tool = AdjustSeverity(incidents=incidents_service, severity=fake_severity)
    ctx = _make_ctx(project_id, agent=ALERT_PRIORITIZATION_AGENT)
    res = await tool.run(
        ctx,
        AdjustSeverityArgs(
            incident_id=inc_id,
            direction="up",
            reason="High business impact observed",
        ),
    )
    assert not res.is_error
    assert "adjusted to high" in res.content

    # Because AdjustSeverity sets by="alert-prioritization", PrioritizationWake
    # inspects incident.severity_changed event, sees source="alert-prioritization",
    # and returns without waking (avoids re-wake loop).
    assert len(fake_sessions.posted_messages) == 4

    # External severity change (e.g. by user or system) DOES trigger a wake
    await incidents_service.set_severity(
        project_id,
        inc_id,
        severity="medium",
        reason="User downgraded severity",
        by="user",
    )
    assert len(fake_sessions.posted_messages) == 5
    _, last_agent, _, last_text = fake_sessions.posted_messages[-1]
    assert last_agent == ALERT_PRIORITIZATION_AGENT
    assert "severity_lowered" in last_text


async def test_two_changes_of_one_kind_wake_twice(
    waker: Waker,
    fake_sessions: FakeSessions,
    event_log: SqliteEventLog,
    incidents_service: IncidentsService,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    """Alert Prioritization is woken for every change of the same kind."""
    prioritization_wake = PrioritizationWake(waker=waker, events=event_log)
    incidents_service.add_observer(prioritization_wake)

    now = fake_clock.now()
    outcome = await incidents_service.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            result_id="res_1",
            subject="pipeline_test",
            category="data_quality",
            model="churn",
            ran_at=now,
            detail={"issue": "test"},
        ),
        seen_at=now,
    )
    inc_id = outcome.incident_id
    assert inc_id is not None
    assert len(fake_sessions.posted_messages) == 1

    # First severity change by user -> wakes
    await incidents_service.set_severity(
        project_id,
        inc_id,
        severity="high",
        reason="First external change",
        by="user",
    )
    assert len(fake_sessions.posted_messages) == 2

    # Second severity change by user of the same kind -> wakes again (two severity changes wake twice)
    await incidents_service.set_severity(
        project_id,
        inc_id,
        severity="critical",
        reason="Second external change",
        by="user",
    )
    assert len(fake_sessions.posted_messages) == 3


async def test_second_adjustment_beyond_one_level_refused(
    incidents_service: IncidentsService,
    fake_severity: FakeSeverityPolicy,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    """A second adjustment beyond one level from policy severity is refused."""
    fake_severity.default_severity = "medium"

    now = fake_clock.now()
    outcome = await incidents_service.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            result_id="res_2",
            subject="pipeline_test",
            category="data_quality",
            model="churn",
            ran_at=now,
            detail={"issue": "test"},
        ),
        seen_at=now,
    )
    inc_id = outcome.incident_id
    assert inc_id is not None

    tool = AdjustSeverity(incidents=incidents_service, severity=fake_severity)
    ctx = _make_ctx(project_id, agent=ALERT_PRIORITIZATION_AGENT)

    # 1. First adjustment up: medium -> high (diff 1 from policy) -> OK
    res1 = await tool.run(
        ctx,
        AdjustSeverityArgs(
            incident_id=inc_id,
            direction="up",
            reason="Elevating due to affected production users",
        ),
    )
    assert not res1.is_error
    assert "adjusted to high" in res1.content

    inc_view = await incidents_service.get(project_id, inc_id)
    assert inc_view is not None
    assert inc_view.severity == "high"

    # 2. Second adjustment up: high -> critical (diff 2 from policy severity medium) -> Refused
    res2 = await tool.run(
        ctx,
        AdjustSeverityArgs(
            incident_id=inc_id,
            direction="up",
            reason="Attempting second bump",
        ),
    )
    assert res2.is_error
    assert res2.content == "Adjustment beyond one level from policy severity is refused."

    # Severity remains high
    inc_view2 = await incidents_service.get(project_id, inc_id)
    assert inc_view2 is not None
    assert inc_view2.severity == "high"

    # 3. Adjustment down back to medium (diff 0 from policy) -> OK
    res3 = await tool.run(
        ctx,
        AdjustSeverityArgs(
            incident_id=inc_id,
            direction="down",
            reason="De-escalating back to medium",
        ),
    )
    assert not res3.is_error
    assert "adjusted to medium" in res3.content

    # 4. Adjustment down from medium to low (diff 1 from policy) -> OK
    res4 = await tool.run(
        ctx,
        AdjustSeverityArgs(
            incident_id=inc_id,
            direction="down",
            reason="Lowering to low",
        ),
    )
    assert not res4.is_error
    assert "adjusted to low" in res4.content

    # 5. Second adjustment down from low -> target rank 0 < 1 -> Refused
    res5 = await tool.run(
        ctx,
        AdjustSeverityArgs(
            incident_id=inc_id,
            direction="down",
            reason="Lowering further",
        ),
    )
    assert res5.is_error
    assert res5.content == "Cannot lower severity below low."


async def test_adjust_severity_bounds_at_critical(
    incidents_service: IncidentsService,
    fake_severity: FakeSeverityPolicy,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    """Adjusting up when already critical is refused."""
    fake_severity.default_severity = "critical"

    now = fake_clock.now()
    outcome = await incidents_service.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            result_id="res_crit",
            subject="pipeline_test",
            category="data_quality",
            model="churn",
            ran_at=now,
            detail={"issue": "test"},
        ),
        seen_at=now,
    )
    inc_id = outcome.incident_id
    assert inc_id is not None

    tool = AdjustSeverity(incidents=incidents_service, severity=fake_severity)
    ctx = _make_ctx(project_id, agent=ALERT_PRIORITIZATION_AGENT)

    res = await tool.run(
        ctx,
        AdjustSeverityArgs(
            incident_id=inc_id,
            direction="up",
            reason="Attempting to go higher than critical",
        ),
    )
    assert res.is_error
    assert res.content == "Cannot raise severity above critical."


async def test_another_agent_is_refused(
    incidents_service: IncidentsService,
    fake_severity: FakeSeverityPolicy,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    """Any agent other than alert-prioritization is refused."""
    now = fake_clock.now()
    outcome = await incidents_service.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            result_id="res_3",
            subject="pipeline_test",
            category="data_quality",
            model="churn",
            ran_at=now,
            detail={"issue": "test"},
        ),
        seen_at=now,
    )
    inc_id = outcome.incident_id
    assert inc_id is not None

    tool = AdjustSeverity(incidents=incidents_service, severity=fake_severity)

    # Calling as data-quality
    ctx = _make_ctx(project_id, agent="data-quality")
    res = await tool.run(
        ctx,
        AdjustSeverityArgs(
            incident_id=inc_id,
            direction="up",
            reason="Data quality trying to adjust",
        ),
    )
    assert res.is_error
    assert res.content == "AdjustSeverity is only permitted for alert-prioritization."

    # Calling as orchestrator
    ctx2 = _make_ctx(project_id, agent="orchestrator")
    res2 = await tool.run(
        ctx2,
        AdjustSeverityArgs(
            incident_id=inc_id,
            direction="up",
            reason="Orchestrator trying to adjust",
        ),
    )
    assert res2.is_error
    assert res2.content == "AdjustSeverity is only permitted for alert-prioritization."


async def test_change_records_incident_severity_changed_with_reason(
    incidents_service: IncidentsService,
    fake_severity: FakeSeverityPolicy,
    fake_clock: FakeClock,
    event_log: SqliteEventLog,
    project_id: str,
) -> None:
    """The change records incident.severity_changed with the reason and source."""
    fake_severity.default_severity = "medium"

    now = fake_clock.now()
    outcome = await incidents_service.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            result_id="res_4",
            subject="pipeline_test",
            category="data_quality",
            model="churn",
            ran_at=now,
            detail={"issue": "test"},
        ),
        seen_at=now,
    )
    inc_id = outcome.incident_id
    assert inc_id is not None

    tool = AdjustSeverity(incidents=incidents_service, severity=fake_severity)
    ctx = _make_ctx(project_id, agent=ALERT_PRIORITIZATION_AGENT)
    reason = "Downstream scoring pipeline latency increasing rapidly"

    res = await tool.run(
        ctx,
        AdjustSeverityArgs(
            incident_id=inc_id,
            direction="up",
            reason=reason,
        ),
    )
    assert not res.is_error

    # Inspect recorded events
    events = await event_log.page(
        project_id,
        subject_id=inc_id,
        types=["incident.severity_changed"],
        limit=5,
    )
    assert len(events) == 1
    event = events[0]
    assert event.source == ALERT_PRIORITIZATION_AGENT
    assert event.type == "incident.severity_changed"
    assert event.payload["before"] == "medium"
    assert event.payload["after"] == "high"
    assert event.payload["reason"] == reason


async def test_adjust_severity_nonexistent_incident(
    incidents_service: IncidentsService,
    fake_severity: FakeSeverityPolicy,
    project_id: str,
) -> None:
    """Adjusting a nonexistent incident returns error."""
    tool = AdjustSeverity(incidents=incidents_service, severity=fake_severity)
    ctx = _make_ctx(project_id, agent=ALERT_PRIORITIZATION_AGENT)

    res = await tool.run(
        ctx,
        AdjustSeverityArgs(
            incident_id="inc_missing",
            direction="up",
            reason="Testing missing incident",
        ),
    )
    assert res.is_error
    assert "Incident 'inc_missing' not found" in res.content


async def test_tool_registry_and_risk_class(
    incidents_service: IncidentsService,
    fake_severity: FakeSeverityPolicy,
) -> None:
    """AdjustSeverity tool is registered in default_registry and has RiskClass.READ."""
    assert AdjustSeverity.risk == RiskClass.READ
    assert AdjustSeverity.name == ADJUST_SEVERITY

    # When both incidents and severity are provided, AdjustSeverity is in registry
    reg = default_registry(incidents=incidents_service, severity=fake_severity)
    assert ADJUST_SEVERITY in reg.names()
    tool = reg.get(ADJUST_SEVERITY)
    assert isinstance(tool, AdjustSeverity)

    # When severity is not provided, AdjustSeverity is not registered
    reg_no_sev = default_registry(incidents=incidents_service)
    assert ADJUST_SEVERITY not in reg_no_sev.names()
