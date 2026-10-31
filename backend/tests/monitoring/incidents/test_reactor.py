"""Tests for Reactor (Task 4 of Plan #464)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx2
import pytest
from sqlalchemy import select

from mlops_agents.agents.tools.issues import set_issue_min_priority
from mlops_agents.credentials.keystore import KeyStore
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.monitoring.expectations.contract import NeverSuspended
from mlops_agents.monitoring.incidents.contract import (
    HoldScope,
    IncidentChange,
    IncidentView,
    Severity,
    SignalDraft,
)
from mlops_agents.monitoring.incidents.dispatch import DiagnosisDispatch
from mlops_agents.monitoring.incidents.from_results import SignalsFromResults
from mlops_agents.monitoring.incidents.reactor import Reactor
from mlops_agents.monitoring.incidents.routing import OnCallRouter
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.monitoring.incidents.severity import RuleSeverity
from mlops_agents.monitoring.waker import Waker
from mlops_agents.notifications.channels import default_channels
from mlops_agents.notifications.contract import Message, OnCallPerson
from mlops_agents.notifications.escalation import EscalationJob
from mlops_agents.notifications.notifier import SqliteNotifier
from mlops_agents.notifications.on_call import SqliteOnCallPeople
from mlops_agents.notifications.settings import IntegrationSettings
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.handoffs import SqliteHandoffs
from mlops_agents.proposals.payloads import (
    Contact,
    HandoffPayload,
    OnCallHours,
)
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import notification_log
from mlops_agents.workspace.refs import RepoRef
from tests.fakes.fake_discord import FakeDiscordState, RunningFakeDiscord, start_fake_discord
from tests.fakes.fake_github import FakeGitHubState, RunningFakeGitHub, start_fake_github
from tests.fakes.fake_tasks import FakeTasks
from tests.monitoring.incidents.conftest import FakeSeverityPolicy, make_stored_result
from tests.proposals.samples import handoff as sample_handoff_dict
from tests.proposals.samples import model as sample_model_dict


class FakeClock(Clock):
    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta

    async def sleep(self, seconds: float) -> None:
        pass


class MemoryKeyStore(KeyStore):
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, ref: str) -> str | None:
        return self.store.get(ref)

    def set(self, ref: str, value: str) -> None:
        self.store[ref] = value

    def delete(self, ref: str) -> None:
        self.store.pop(ref, None)


class FakeResults:
    def __init__(self) -> None:
        self.records: list[Any] = []
        self.latest_results: list[Any] = []

    async def latest_per_check(self, project_id: str, subject: str, model: str | None) -> list[Any]:
        return self.latest_results


class FakeSessions:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post(
        self,
        project_id: str,
        agent: str,
        *,
        author_name: str,
        from_agent: str,
        text: str,
        exchange_id: str | None = None,
        hop: int | None = None,
        system: bool = False,
    ) -> None:
        self.posts.append(
            {
                "project_id": project_id,
                "agent": agent,
                "author_name": author_name,
                "from_agent": from_agent,
                "text": text,
                "exchange_id": exchange_id,
                "hop": hop,
                "system": system,
            }
        )


class DummyGitHubAuth(GitHubAuth):
    def __init__(self, token_val: str | None = None) -> None:
        self._token = token_val

    async def token(self) -> str | None:
        return self._token


class TaskDouble(FakeTasks):
    def __init__(self, repo_ref: RepoRef | None = None) -> None:
        super().__init__()
        self._repo = repo_ref

    def set_remote(self, project_id: str, owner: str, repo: str) -> None:
        self._repo = RepoRef(owner=owner, repo=repo)

    async def repo_ref(self, project_id: str) -> RepoRef | None:
        return self._repo


@pytest.fixture
def fake_discord() -> Iterator[RunningFakeDiscord]:
    state = FakeDiscordState()
    server = start_fake_discord(state)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def fake_github() -> Iterator[RunningFakeGitHub]:
    state = FakeGitHubState()
    server = start_fake_github(state)
    try:
        yield server
    finally:
        server.stop()


def _make_on_call_person(
    id: str = "alice",
    name: str = "Alice",
    order: int = 0,
    channel: str = "discord",
    target: str = "user_123",
) -> OnCallPerson:
    return OnCallPerson(
        id=id,
        name=name,
        contacts=[Contact(channel=channel, address=target)],  # type: ignore[arg-type]
        hours=OnCallHours(
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            start="09:00",
            end="17:00",
        ),
        timezone="UTC",
        escalation_order=order,
    )


@pytest.mark.anyio
async def test_production_signal_ci_job_opens_critical_notifies_discord_and_wakes(
    tmp_path: Any,
    fake_discord: RunningFakeDiscord,
) -> None:
    project_id = "prj_reactor1"
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    databases = Databases(paths)
    keystore = MemoryKeyStore()
    events = SqliteEventLog(databases)
    clock = FakeClock()
    http = httpx2.AsyncClient()

    # Configure Discord integration
    settings = IntegrationSettings(databases, keystore)
    await settings.set_discord(
        enabled=True,
        webhook_url=f"{fake_discord.base_url}/api/webhooks/123/token123",
    )
    channels = default_channels(settings, http)
    notifier = SqliteNotifier(databases, settings, channels, events, clock)

    on_call = SqliteOnCallPeople(databases)
    person = _make_on_call_person(id="alice", name="Alice", order=0)
    await on_call.replace(project_id, [person])

    # Model with production_signal of kind ci_job
    m_dict = sample_model_dict(name="churn_model")
    m_dict["production_signal"] = {"kind": "ci_job", "ref": "score.yml"}
    handoff_dict = sample_handoff_dict(models=[m_dict])
    payload = HandoffPayload.model_validate(handoff_dict)

    handoffs = SqliteHandoffs(databases)
    await handoffs.store(project_id, "prop_1", payload)

    severity = RuleSeverity(handoffs=handoffs)
    results = FakeResults()
    incidents = IncidentsService(
        databases=databases,
        events=events,
        results=results,  # type: ignore[arg-type]
        severity=severity,
        maintenance=NeverSuspended(),
        clock=clock,
        handoffs=handoffs,
    )
    severity.bind_incidents(incidents)

    sessions = FakeSessions()
    waker = Waker(databases, events, clock)
    waker.bind(sessions)  # type: ignore[arg-type]
    dispatch = DiagnosisDispatch(incidents, waker)

    router = OnCallRouter(on_call=on_call, databases=databases)
    tasks = TaskDouble()
    github = DummyGitHubAuth()

    # We track call order to assert notifier was called before diagnoser was woken
    call_order: list[str] = []
    original_notify = notifier.notify

    async def tracking_notify(*args: Any, **kwargs: Any) -> Any:
        call_order.append("notify")
        return await original_notify(*args, **kwargs)

    notifier.notify = tracking_notify  # type: ignore[method-assign]

    original_wake = waker.wake

    async def tracking_wake(*args: Any, **kwargs: Any) -> Any:
        call_order.append("wake")
        return await original_wake(*args, **kwargs)

    waker.wake = tracking_wake  # type: ignore[method-assign]

    reactor = Reactor(
        incidents=incidents,
        router=router,
        on_call=on_call,
        notifier=notifier,
        dispatch=dispatch,
        tasks=tasks,
        github=github,
        http=http,
        events=events,
        clock=clock,
    )
    incidents.add_observer(reactor)

    # Raise a fired result from the production signal CI job
    draft = SignalDraft(
        kind="fired_result",
        category="operational",
        subject="score.yml",
        model="churn_model",
        ran_at=clock.now(),
        detail={"run_id": "123"},
        source_ref="ci:score.yml",
    )
    outcome = await incidents.raise_signal(project_id, draft, clock.now())

    # Verify incident severity is critical
    assert outcome.change == "opened"
    assert outcome.severity == "critical"

    inc = await incidents.get(project_id, outcome.incident_id)
    assert inc is not None
    assert inc.subtype == "failed_scoring"
    assert inc.severity == "critical"

    # Verify call order: notifier called BEFORE waker was called
    assert "notify" in call_order
    assert "wake" in call_order
    assert call_order.index("notify") < call_order.index("wake")

    # Verify notification reached fake Discord
    assert len(fake_discord.state.posts) == 1
    post = fake_discord.state.posts[0]
    assert "CRITICAL" in post["content"]
    assert inc.id in post["content"]

    # Verify diagnoser was woken
    assert len(sessions.posts) == 1
    wake_post = sessions.posts[0]
    assert wake_post["agent"] == "deployment"
    assert wake_post["from_agent"] == "system"
    assert inc.id in wake_post["text"]

    await http.aclose()


@pytest.mark.anyio
async def test_severity_rise_in_hours_sends_once_and_quiet_attach_sends_nothing(
    tmp_path: Any,
) -> None:
    project_id = "prj_reactor2"
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    databases = Databases(paths)
    events = SqliteEventLog(databases)
    clock = FakeClock()
    http = httpx2.AsyncClient()

    fake_notifier = AsyncMock()
    fake_on_call = AsyncMock()
    person = _make_on_call_person(id="bob", name="Bob", order=0)
    fake_on_call.list.return_value = [person]

    handoffs = AsyncMock()
    handoffs.current.return_value = None

    class DynamicSeverity:
        def __init__(self) -> None:
            self.severity: Severity = "medium"

        async def decide(self, project_id: str, incident: IncidentView) -> Any:
            from mlops_agents.monitoring.incidents.contract import SeverityDecision

            return SeverityDecision(severity=self.severity, reasons=[])

    dyn_sev = DynamicSeverity()
    results = FakeResults()
    incidents = IncidentsService(
        databases=databases,
        events=events,
        results=results,  # type: ignore[arg-type]
        severity=dyn_sev,  # type: ignore[arg-type]
        maintenance=NeverSuspended(),
        clock=clock,
        handoffs=handoffs,
    )

    fake_dispatch = AsyncMock()
    router = OnCallRouter(on_call=fake_on_call, databases=databases)
    tasks = TaskDouble()
    github = DummyGitHubAuth()

    reactor = Reactor(
        incidents=incidents,
        router=router,
        on_call=fake_on_call,
        notifier=fake_notifier,
        dispatch=fake_dispatch,
        tasks=tasks,
        github=github,
        http=http,
        events=events,
        clock=clock,
    )
    incidents.add_observer(reactor)

    # 1. Open at medium: medium doesn't page on-call (deliveries is empty)
    draft1 = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="users",
        ran_at=clock.now(),
        detail={},
    )
    outcome1 = await incidents.raise_signal(project_id, draft1, clock.now())
    assert outcome1.change == "opened"
    assert outcome1.severity == "medium"
    assert fake_notifier.notify.call_count == 0

    # 2. Quiet attach: second signal with same severity medium returns change="quiet"
    draft2 = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="users",
        ran_at=clock.now(),
        detail={"extra": 1},
    )
    outcome2 = await incidents.raise_signal(project_id, draft2, clock.now())
    assert outcome2.change == "quiet"
    assert fake_notifier.notify.call_count == 0

    # 3. Severity rise: severity raised to high in hours
    dyn_sev.severity = "high"
    draft3 = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="users",
        ran_at=clock.now(),
        detail={"extra": 2},
    )
    outcome3 = await incidents.raise_signal(project_id, draft3, clock.now())
    assert outcome3.change == "attached"
    assert outcome3.severity == "high"

    # Verify notification was sent once for the severity rise
    assert fake_notifier.notify.call_count == 1
    call_args = fake_notifier.notify.call_args
    assert call_args.args[1].id == "bob"
    msg: Message = call_args.args[2]
    assert msg.severity == "high"

    await http.aclose()


@pytest.mark.anyio
async def test_notifier_failure_still_wakes(tmp_path: Any) -> None:
    project_id = "prj_reactor3"
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    databases = Databases(paths)
    events = SqliteEventLog(databases)
    clock = FakeClock()
    http = httpx2.AsyncClient()

    fake_notifier = AsyncMock()
    fake_notifier.notify.side_effect = RuntimeError("Discord server down")

    person = _make_on_call_person(id="charlie", name="Charlie", order=0)
    fake_on_call = AsyncMock()
    fake_on_call.list.return_value = [person]

    fake_handoffs = AsyncMock()
    fake_handoffs.current.return_value = None

    class CritSeverity:
        async def decide(self, project_id: str, incident: IncidentView) -> Any:
            from mlops_agents.monitoring.incidents.contract import SeverityDecision

            return SeverityDecision(severity="critical", reasons=["Critical"])

    incidents = IncidentsService(
        databases=databases,
        events=events,
        results=FakeResults(),  # type: ignore[arg-type]
        severity=CritSeverity(),  # type: ignore[arg-type]
        maintenance=NeverSuspended(),
        clock=clock,
        handoffs=fake_handoffs,
    )

    fake_dispatch = AsyncMock()
    router = OnCallRouter(on_call=fake_on_call, databases=databases)
    tasks = TaskDouble()
    github = DummyGitHubAuth()

    reactor = Reactor(
        incidents=incidents,
        router=router,
        on_call=fake_on_call,
        notifier=fake_notifier,
        dispatch=fake_dispatch,
        tasks=tasks,
        github=github,
        http=http,
        events=events,
        clock=clock,
    )
    incidents.add_observer(reactor)

    draft = SignalDraft(
        kind="fired_result",
        category="operational",
        subject="train.py",
        ran_at=clock.now(),
        detail={},
    )
    outcome = await incidents.raise_signal(project_id, draft, clock.now())
    assert outcome.severity == "critical"

    # Notifier was called and failed
    assert fake_notifier.notify.call_count == 1

    # But dispatch.changed was still called!
    assert fake_dispatch.changed.call_count == 1
    change: IncidentChange = fake_dispatch.changed.call_args.args[1]
    assert change.incident_id == outcome.incident_id
    assert change.kind == "opened"

    await http.aclose()


@pytest.mark.anyio
async def test_issue_opens_at_and_above_threshold_once_per_incident(
    tmp_path: Any,
    fake_github: RunningFakeGitHub,
) -> None:
    project_id = "prj_reactor4"
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    databases = Databases(paths)
    events = SqliteEventLog(databases)
    clock = FakeClock()
    http = httpx2.AsyncClient()

    fake_notifier = AsyncMock()
    fake_on_call = AsyncMock()
    fake_on_call.list.return_value = []
    fake_handoffs = AsyncMock()
    fake_handoffs.current.return_value = None

    class DynamicSeverity:
        def __init__(self) -> None:
            self.severity: Severity = "low"

        async def decide(self, project_id: str, incident: IncidentView) -> Any:
            from mlops_agents.monitoring.incidents.contract import SeverityDecision

            return SeverityDecision(severity=self.severity, reasons=[])

    dyn_sev = DynamicSeverity()
    incidents = IncidentsService(
        databases=databases,
        events=events,
        results=FakeResults(),  # type: ignore[arg-type]
        severity=dyn_sev,  # type: ignore[arg-type]
        maintenance=NeverSuspended(),
        clock=clock,
        handoffs=fake_handoffs,
    )

    fake_dispatch = AsyncMock()
    router = OnCallRouter(on_call=fake_on_call, databases=databases)

    # Set threshold to "high"
    await set_issue_min_priority(databases, project_id, "high")

    tasks = TaskDouble()
    tasks.set_remote("prj_reactor4", "acme", "pipeline")

    class FakeGitHubAuth(GitHubAuth):
        def __init__(self) -> None:
            pass

        async def token(self) -> str | None:
            return "ghp_fake_token_123"

    github = FakeGitHubAuth()

    # Point httpx to fake github base url via monkeypatch or mock
    reactor = Reactor(
        incidents=incidents,
        router=router,
        on_call=fake_on_call,
        notifier=fake_notifier,
        dispatch=fake_dispatch,
        tasks=tasks,
        github=github,
        http=http,
        events=events,
        clock=clock,
    )
    incidents.add_observer(reactor)

    # Mock create_issue and ensure_labels so we inspect exact calls and number
    created_issue_num = 42
    mock_create = AsyncMock()
    from mlops_agents.integrations.github.issues import IssueSummary

    mock_create.return_value = IssueSummary(
        number=created_issue_num,
        title="Incident Title",
        html_url="https://github.com/acme/pipeline/issues/42",
        state="open",
        labels=["category:data_quality", "priority:high"],
    )
    mock_ensure = AsyncMock()

    with (
        patch("mlops_agents.monitoring.incidents.reactor.create_issue", mock_create),
        patch("mlops_agents.monitoring.incidents.reactor.ensure_labels", mock_ensure),
    ):
        # 1. Incident at severity "low" (< threshold "high") -> opens no issue
        dyn_sev.severity = "low"
        draft_low = SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="users",
            ran_at=clock.now(),
            detail={},
        )
        outcome_low = await incidents.raise_signal(project_id, draft_low, clock.now())
        assert outcome_low.severity == "low"
        assert mock_create.call_count == 0

        # 2. Raise severity to "high" (>= threshold "high") -> opens issue
        dyn_sev.severity = "high"
        draft_high = SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="users",
            ran_at=clock.now(),
            detail={"level": 2},
        )
        outcome_high = await incidents.raise_signal(project_id, draft_high, clock.now())
        assert outcome_high.severity == "high"
        assert mock_create.call_count == 1

        # Verify incident has issues list recorded
        inc_after = await incidents.get(project_id, outcome_low.incident_id)
        assert inc_after is not None
        assert inc_after.issues == [42]

        # Verify incident.updated event was recorded
        ev_page = await events.page(project_id, subject_id=inc_after.id, limit=5)
        update_ev = next((e for e in ev_page if e.type == "incident.updated"), None)
        assert update_ev is not None
        assert update_ev.payload == {"changed": {"issues": [42]}}

        # 3. Another event on the same incident (e.g. severity to critical) -> does NOT open a second issue
        dyn_sev.severity = "critical"
        draft_crit = SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="users",
            ran_at=clock.now(),
            detail={"level": 3},
        )
        await incidents.raise_signal(project_id, draft_crit, clock.now())
        assert mock_create.call_count == 1  # Still 1, not 2!

    await http.aclose()


@pytest.mark.anyio
async def test_snoozed_incident_sends_nothing(tmp_path: Any) -> None:
    project_id = "prj_reactor5"
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    databases = Databases(paths)
    events = SqliteEventLog(databases)
    clock = FakeClock()
    http = httpx2.AsyncClient()

    fake_notifier = AsyncMock()
    fake_on_call = AsyncMock()
    fake_on_call.list.return_value = [_make_on_call_person()]
    fake_handoffs = AsyncMock()
    fake_handoffs.current.return_value = None

    class CritSeverity:
        async def decide(self, project_id: str, incident: IncidentView) -> Any:
            from mlops_agents.monitoring.incidents.contract import SeverityDecision

            return SeverityDecision(severity="critical", reasons=[])

    incidents = IncidentsService(
        databases=databases,
        events=events,
        results=FakeResults(),  # type: ignore[arg-type]
        severity=CritSeverity(),  # type: ignore[arg-type]
        maintenance=NeverSuspended(),
        clock=clock,
        handoffs=fake_handoffs,
    )

    fake_dispatch = AsyncMock()
    router = OnCallRouter(on_call=fake_on_call, databases=databases)
    tasks = TaskDouble()
    github = DummyGitHubAuth()

    reactor = Reactor(
        incidents=incidents,
        router=router,
        on_call=fake_on_call,
        notifier=fake_notifier,
        dispatch=fake_dispatch,
        tasks=tasks,
        github=github,
        http=http,
        events=events,
        clock=clock,
    )
    incidents.add_observer(reactor)

    # 1. Open incident
    draft = SignalDraft(
        kind="fired_result",
        category="operational",
        subject="job.py",
        ran_at=clock.now(),
        detail={},
    )
    outcome = await incidents.raise_signal(project_id, draft, clock.now())
    assert fake_notifier.notify.call_count == 1
    assert fake_dispatch.changed.call_count == 1

    # Reset mocks
    fake_notifier.reset_mock()
    fake_dispatch.reset_mock()

    # 2. Snooze incident for 2 hours
    await incidents.snooze(
        project_id, outcome.incident_id, clock.now() + timedelta(hours=2), by="user"
    )

    # 3. Simulate change while snoozed (e.g. severity raised or reopened or hold)
    change = IncidentChange(
        incident_id=outcome.incident_id,
        kind="severity_raised",
        before="high",
        after="critical",
    )
    await reactor.changed(project_id, change)

    # Snoozed incident sends nothing and wakes nothing!
    assert fake_notifier.notify.call_count == 0
    assert fake_dispatch.changed.call_count == 0

    await http.aclose()


@pytest.mark.anyio
async def test_hold_change_sends_notification_and_does_not_wake(tmp_path: Any) -> None:
    project_id = "prj_reactor6"
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    databases = Databases(paths)
    events = SqliteEventLog(databases)
    clock = FakeClock()
    http = httpx2.AsyncClient()

    fake_notifier = AsyncMock()
    person = _make_on_call_person(id="dave", name="Dave", order=0)
    fake_on_call = AsyncMock()
    fake_on_call.list.return_value = [person]
    fake_handoffs = AsyncMock()
    fake_handoffs.current.return_value = None

    class CritSeverity:
        async def decide(self, project_id: str, incident: IncidentView) -> Any:
            from mlops_agents.monitoring.incidents.contract import SeverityDecision

            return SeverityDecision(severity="critical", reasons=[])

    incidents = IncidentsService(
        databases=databases,
        events=events,
        results=FakeResults(),  # type: ignore[arg-type]
        severity=CritSeverity(),  # type: ignore[arg-type]
        maintenance=NeverSuspended(),
        clock=clock,
        handoffs=fake_handoffs,
    )

    fake_dispatch = AsyncMock()
    router = OnCallRouter(on_call=fake_on_call, databases=databases)
    tasks = TaskDouble()
    github = DummyGitHubAuth()

    reactor = Reactor(
        incidents=incidents,
        router=router,
        on_call=fake_on_call,
        notifier=fake_notifier,
        dispatch=fake_dispatch,
        tasks=tasks,
        github=github,
        http=http,
        events=events,
        clock=clock,
    )
    incidents.add_observer(reactor)

    draft = SignalDraft(
        kind="fired_result",
        category="operational",
        subject="job.py",
        ran_at=clock.now(),
        detail={},
    )
    outcome = await incidents.raise_signal(project_id, draft, clock.now())
    fake_notifier.reset_mock()
    fake_dispatch.reset_mock()

    # Record hold
    scope = HoldScope(tables=["users", "orders"])
    await incidents.record_hold(
        project_id, outcome.incident_id, scope, reason="Bad data batch", by="agent"
    )

    # Notification was sent with hold lines describing what was held
    assert fake_notifier.notify.call_count == 1
    msg: Message = fake_notifier.notify.call_args.args[2]
    assert any("users" in line and "orders" in line for line in msg.lines)

    # Diagnoser was NOT woken for hold!
    assert fake_dispatch.changed.call_count == 0

    await http.aclose()


@pytest.mark.anyio
async def test_cause_change_only_calls_dispatch(tmp_path: Any) -> None:
    project_id = "prj_reactor7"
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    databases = Databases(paths)
    events = SqliteEventLog(databases)
    clock = FakeClock()
    http = httpx2.AsyncClient()

    fake_notifier = AsyncMock()
    fake_on_call = AsyncMock()
    fake_handoffs = AsyncMock()
    fake_handoffs.current.return_value = None

    incidents = IncidentsService(
        databases=databases,
        events=events,
        results=FakeResults(),  # type: ignore[arg-type]
        severity=AsyncMock(),
        maintenance=NeverSuspended(),
        clock=clock,
        handoffs=fake_handoffs,
    )

    fake_dispatch = AsyncMock()
    router = OnCallRouter(on_call=fake_on_call, databases=databases)
    tasks = TaskDouble()
    github = DummyGitHubAuth()

    reactor = Reactor(
        incidents=incidents,
        router=router,
        on_call=fake_on_call,
        notifier=fake_notifier,
        dispatch=fake_dispatch,
        tasks=tasks,
        github=github,
        http=http,
        events=events,
        clock=clock,
    )

    change = IncidentChange(incident_id="inc_xyz", kind="cause")
    await reactor.changed(project_id, change)

    # Dispatch was called, notifier was not called
    assert fake_dispatch.changed.call_count == 1
    assert fake_notifier.notify.call_count == 0

    await http.aclose()


async def test_reopen_clears_resolved_offline_and_pages_and_wakes(
    databases: Databases,
    event_log: SqliteEventLog,
    fake_clock: FakeClock,
    project_id: str,
) -> None:
    fake_notifier = AsyncMock()
    fake_dispatch = AsyncMock()
    fake_on_call = AsyncMock()
    fake_handoffs = AsyncMock()
    fake_handoffs.current.return_value = None

    person = _make_on_call_person("alice")
    fake_on_call.list.return_value = [person]

    results = FakeResults()
    incidents = IncidentsService(
        databases=databases,
        events=event_log,
        results=results,  # type: ignore[arg-type]
        severity=FakeSeverityPolicy("high"),
        maintenance=NeverSuspended(),
        clock=fake_clock,
        handoffs=fake_handoffs,
    )
    router = OnCallRouter(on_call=fake_on_call, databases=databases)
    tasks = TaskDouble()
    github = DummyGitHubAuth()
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(lambda req: httpx2.Response(200)))

    reactor = Reactor(
        incidents=incidents,
        router=router,
        on_call=fake_on_call,
        notifier=fake_notifier,
        dispatch=fake_dispatch,
        tasks=tasks,
        github=github,
        http=http,
        events=event_log,
        clock=fake_clock,
    )
    incidents.add_observer(reactor)

    signals_from_results = SignalsFromResults(
        incidents=incidents,
        maintenance=NeverSuspended(),
        results=results,  # type: ignore[arg-type]
    )

    t0 = datetime(2026, 10, 30, 2, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 10, 30, 3, 0, 0, tzinfo=UTC)
    r_fired = make_stored_result(
        "res_fired_0200",
        check_id="chk_offline",
        subject="orders",
        model="churn",
        fired=True,
        ran_at=t0,
        seen_at=t1,
    )
    r_cleared = make_stored_result(
        "res_cleared_0300",
        check_id="chk_offline",
        subject="orders",
        model="churn",
        fired=False,
        ran_at=t1,
        seen_at=t1,
    )
    results.latest_results = [r_cleared]
    await signals_from_results.stored(project_id, [r_fired, r_cleared])

    page = await incidents.list(project_id)
    assert len(page.rows) == 1
    inc = page.rows[0]
    assert inc.status == "resolved"
    assert inc.resolved_offline is True
    # Initial offline resolve: no page, no wake
    assert fake_notifier.notify.call_count == 0
    assert fake_dispatch.changed.call_count == 0

    # Fresh fire arrives within reopen window
    t2 = datetime(2026, 10, 30, 4, 0, 0, tzinfo=UTC)
    r_fresh = make_stored_result(
        "res_fresh_0400",
        check_id="chk_offline",
        subject="orders",
        model="churn",
        fired=True,
        ran_at=t2,
        seen_at=t2,
    )
    results.latest_results = [r_fresh]
    await signals_from_results.stored(project_id, [r_fresh])

    reopened_inc = await incidents.get(project_id, inc.id)
    assert reopened_inc is not None
    assert reopened_inc.status == "reopened"
    assert reopened_inc.resolved_offline is False

    # Now paged and woken!
    assert fake_notifier.notify.call_count >= 1
    assert fake_dispatch.changed.call_count >= 1

    await http.aclose()


@pytest.mark.anyio
async def test_merge_critical_into_medium_keeps_critical_no_duplicate_page_and_escalates(
    tmp_path: Any,
    fake_discord: RunningFakeDiscord,
) -> None:
    """Merging a critical incident into a medium incident gives a critical winner and a severity change event,

    with no duplicate page to anyone already paged for the critical and escalation continuing to the next person.
    """
    paths = DataPaths(tmp_path)
    paths.ensure()
    databases = Databases(paths)
    project_id = "prj_mergecrit"
    clock = FakeClock(start=datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC))
    events = SqliteEventLog(databases)
    keystore = MemoryKeyStore()
    settings = IntegrationSettings(databases, keystore)
    http = httpx2.AsyncClient()

    await settings.set_discord(
        enabled=True,
        webhook_url=f"{fake_discord.base_url}/api/webhooks/123/token123",
    )
    channels = default_channels(settings, http)
    notifier = SqliteNotifier(databases, settings, channels, events, clock)

    on_call = SqliteOnCallPeople(databases)
    alice = _make_on_call_person(id="alice", name="Alice", order=0)
    bob = _make_on_call_person(id="bob", name="Bob", order=1)
    await on_call.replace(project_id, [alice, bob])

    severity_policy = FakeSeverityPolicy(default_severity="medium")
    results = FakeResults()
    incidents = IncidentsService(
        databases=databases,
        events=events,
        results=results,  # type: ignore[arg-type]
        severity=severity_policy,  # type: ignore[arg-type]
        maintenance=NeverSuspended(),
        clock=clock,
    )

    sessions = FakeSessions()
    waker = Waker(databases, events, clock)
    waker.bind(sessions)  # type: ignore[arg-type]
    dispatch = DiagnosisDispatch(incidents, waker)

    router = OnCallRouter(on_call=on_call, databases=databases)
    tasks = TaskDouble()
    github = DummyGitHubAuth()

    reactor = Reactor(
        incidents=incidents,
        router=router,
        on_call=on_call,
        notifier=notifier,
        dispatch=dispatch,
        tasks=tasks,
        github=github,
        http=http,
        events=events,
        clock=clock,
    )
    incidents.add_observer(reactor)

    escalation_job = EscalationJob(
        databases=databases,
        incidents=incidents,
        on_call=on_call,
        notifier=notifier,
        clock=clock,
    )

    # 1. Raise incident 1 (medium) 20 min ago
    t_med = clock.now() - timedelta(minutes=20)
    d1 = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="tbl_med",
        model=None,
        ran_at=t_med,
        detail={},
    )
    out1 = await incidents.raise_signal(project_id, d1, seen_at=t_med)
    inc1_id = out1.incident_id
    # Medium severity does not trigger immediate page
    assert len(fake_discord.state.posts) == 0

    # 2. Raise incident 2 (critical) 10 min ago
    severity_policy.default_severity = "critical"
    t_crit = clock.now() - timedelta(minutes=10)
    d2 = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="tbl_crit",
        model=None,
        ran_at=t_crit,
        detail={},
    )
    out2 = await incidents.raise_signal(project_id, d2, seen_at=t_crit)
    inc2_id = out2.incident_id
    # Critical severity immediately pages Alice (order 0)
    assert len(fake_discord.state.posts) == 1
    alice_msg = fake_discord.state.posts[0]
    assert "[CRITICAL]" in alice_msg["content"]
    assert inc2_id in alice_msg["content"]

    # 3. Merge inc1 and inc2.
    # inc1 is older, so it is the winner.
    winner = await incidents.merge(project_id, [inc1_id, inc2_id], agent="lead")
    assert winner.id == inc1_id
    assert winner.severity == "critical"

    # Verify event recorded
    evs = await events.page(project_id, subject_id=inc1_id)
    sev_changes = [e for e in evs if e.type == "incident.severity_changed"]
    assert any(
        e.payload.get("before") == "medium" and e.payload.get("after") == "critical"
        for e in sev_changes
    )

    # Critical winner's severity rise did NOT page Alice again (she was already paged for the critical incident)
    assert len(fake_discord.state.posts) == 1

    # 4. Advance clock by 15 minutes (escalation timeout)
    clock.advance(timedelta(minutes=15))
    dues = await escalation_job.due(
        project_id,
        after=clock.now() - timedelta(minutes=30),
        now=clock.now(),
    )
    assert len(dues) == 1
    assert dues[0].subject == inc1_id

    # 5. Execute escalation: escalation continues to Bob (the next person)
    await escalation_job.run(project_id, dues[0])
    assert len(fake_discord.state.posts) == 2
    bob_msg = fake_discord.state.posts[1]
    assert "[CRITICAL]" in bob_msg["content"]

    # Verify notification_log has exactly 2 rows for winner: Alice (incident) and Bob (escalation)
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(notification_log).where(notification_log.c.incident == inc1_id)
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 2
        people = {r["person"]: r["kind"] for r in rows}
        assert people == {"alice": "incident", "bob": "escalation"}

    await http.aclose()
