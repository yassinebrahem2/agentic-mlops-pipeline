"""Tests for incident lifecycle, update, merge, split, holds, snooze, acknowledge, close."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Literal

import pytest
from sqlalchemy import select

from mlops_agents.errors import DomainError
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.incidents.contract import (
    HoldScope,
    IncidentChange,
    IncidentObserver,
    IncidentPatch,
    SignalDraft,
)
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import notification_log
from tests.monitoring.incidents.conftest import (
    FakeClock,
    FakeMaintenance,
    FakeResults,
    FakeSeverityPolicy,
    make_stored_result,
)


class RecordingObserver(IncidentObserver):
    def __init__(self) -> None:
        self.changes: list[IncidentChange] = []

    async def changed(self, project_id: str, change: IncidentChange) -> None:
        self.changes.append(change)


@pytest.fixture
def service(
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


@pytest.mark.asyncio
async def test_lifecycle_transitions_set_timestamps_and_events(
    service: IncidentsService,
    event_log: SqliteEventLog,
    project_id: str,
    fake_clock: FakeClock,
) -> None:
    now = fake_clock.now()
    draft = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="table_a",
        model="model_a",
        ran_at=now - timedelta(minutes=10),
        detail={"null_count": 50},
    )
    outcome = await service.raise_signal(project_id, draft, seen_at=now)
    inc_id = outcome.incident_id

    # Acknowledge
    fake_clock.advance(60)
    ack_time = fake_clock.now()
    inc = await service.acknowledge(project_id, inc_id, by="user")
    assert inc.status == "acknowledged"
    assert inc.acknowledged_at == ack_time

    events = await event_log.page(project_id, subject_id=inc_id)
    assert any(e.type == "incident.acknowledged" for e in events)

    # Snooze
    fake_clock.advance(60)
    snooze_until = fake_clock.now() + timedelta(hours=2)
    inc = await service.snooze(project_id, inc_id, until=snooze_until, by="user")
    assert inc.snoozed_until == snooze_until

    events = await event_log.page(project_id, subject_id=inc_id)
    assert any(e.type == "incident.snoozed" for e in events)

    # Close
    fake_clock.advance(60)
    close_time = fake_clock.now()
    inc = await service.close(project_id, inc_id, by="user")
    assert inc.status == "closed"
    assert inc.closed_at == close_time

    events = await event_log.page(project_id, subject_id=inc_id)
    assert any(e.type == "incident.closed" for e in events)


@pytest.mark.asyncio
async def test_record_hold_and_holds_query(
    service: IncidentsService,
    project_id: str,
    fake_clock: FakeClock,
) -> None:
    now = fake_clock.now()
    obs = RecordingObserver()
    service.add_observer(obs)

    # Incident 1 for model_a
    d1 = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="table_1",
        model="model_a",
        ran_at=now,
        detail={},
    )
    out1 = await service.raise_signal(project_id, d1, seen_at=now)

    # Incident 2 model-less
    d2 = SignalDraft(
        kind="fired_result",
        category="operational",
        subject="global_pipeline",
        model=None,
        ran_at=now,
        detail={},
    )
    out2 = await service.raise_signal(project_id, d2, seen_at=now)

    # Incident 3 for model_b (will be closed)
    d3 = SignalDraft(
        kind="fired_result",
        category="drift",
        subject="table_b",
        model="model_b",
        ran_at=now,
        detail={},
    )
    out3 = await service.raise_signal(project_id, d3, seen_at=now)

    scope1 = HoldScope(tables=["table_1"], start=now - timedelta(days=1), end=now)
    inc1 = await service.record_hold(
        project_id, out1.incident_id, scope=scope1, reason="bad data", by="agent"
    )
    assert inc1.held is True

    scope2 = HoldScope(tables=[], start=None, end=None)
    await service.record_hold(
        project_id, out2.incident_id, scope=scope2, reason="system hold", by="agent"
    )

    scope3 = HoldScope(tables=["table_b"], start=now, end=now)
    await service.record_hold(
        project_id, out3.incident_id, scope=scope3, reason="closed hold", by="agent"
    )
    await service.close(project_id, out3.incident_id, by="user")

    # Observer got hold changes
    hold_changes = [c for c in obs.changes if c.kind == "hold"]
    assert len(hold_changes) == 3

    # holds(model_a) should return scopes for model_a and model-less, but not model_b (and not closed)
    holds_a = await service.holds(project_id, model="model_a")
    assert len(holds_a) == 2
    assert any(h.tables == ["table_1"] for h in holds_a)
    assert any(h.tables == [] for h in holds_a)

    # holds(None) should return only model-less
    holds_none = await service.holds(project_id, model=None)
    assert len(holds_none) == 1
    assert holds_none[0].tables == []


@pytest.mark.asyncio
async def test_update_mitigated_and_resolved_with_latest_firing_checks(
    service: IncidentsService,
    fake_results: FakeResults,
    event_log: SqliteEventLog,
    project_id: str,
    fake_clock: FakeClock,
) -> None:
    now = fake_clock.now()
    draft = SignalDraft(
        kind="fired_result",
        category="model_performance",
        subject="churn_eval",
        model="churn",
        ran_at=now,
        detail={"roc_auc": 0.55},
    )
    outcome = await service.raise_signal(project_id, draft, seen_at=now)
    inc_id = outcome.incident_id

    # Mitigated works
    fake_clock.advance(30)
    inc_mit = await service.update(
        project_id,
        inc_id,
        agent="agent_1",
        patch=IncidentPatch(status="mitigated"),
    )
    assert inc_mit.status == "mitigated"
    assert inc_mit.mitigated_at is not None

    # Resolve while latest result fires is refused
    firing_res = make_stored_result(
        "res_latest",
        fired=True,
        subject="churn_eval",
        model="churn",
    )
    fake_results.latest_results = [firing_res]

    with pytest.raises(DomainError) as exc_info:
        await service.update(
            project_id,
            inc_id,
            agent="agent_1",
            patch=IncidentPatch(status="resolved"),
        )
    assert exc_info.value.status == 409
    assert exc_info.value.code == "incident_still_firing"

    # Now latest result clears (no longer fires)
    fake_results.latest_results = [
        make_stored_result(
            "res_cleared",
            fired=False,
            subject="churn_eval",
            model="churn",
        )
    ]
    fake_clock.advance(30)
    inc_res = await service.update(
        project_id,
        inc_id,
        agent="agent_1",
        patch=IncidentPatch(status="resolved"),
    )
    assert inc_res.status == "resolved"
    assert inc_res.resolved_at is not None

    events = await event_log.page(project_id, subject_id=inc_id)
    assert any(
        e.type == "incident.updated" and e.payload.get("changed", {}).get("status") == "resolved"
        for e in events
    )


@pytest.mark.asyncio
async def test_update_resolved_refused_for_silence_incident(
    service: IncidentsService,
    fake_results: FakeResults,
    project_id: str,
    fake_clock: FakeClock,
) -> None:
    now = fake_clock.now()
    draft = SignalDraft(
        kind="silence",
        category="data_quality",
        subject="feed_clicks",
        model=None,
        ran_at=now,
        detail={"missed_deadline": now.isoformat()},
    )
    outcome = await service.raise_signal(project_id, draft, seen_at=now)

    fake_results.latest_results = []  # no firing results

    with pytest.raises(DomainError) as exc_info:
        await service.update(
            project_id,
            outcome.incident_id,
            agent="agent_1",
            patch=IncidentPatch(status="resolved"),
        )
    assert exc_info.value.status == 409
    assert exc_info.value.code == "incident_still_firing"


@pytest.mark.asyncio
async def test_merge_incidents(
    service: IncidentsService,
    event_log: SqliteEventLog,
    project_id: str,
    fake_clock: FakeClock,
) -> None:
    now = fake_clock.now()
    d1 = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="s1",
        model=None,
        ran_at=now - timedelta(minutes=20),
        detail={},
    )
    d2 = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="s2",
        model=None,
        ran_at=now - timedelta(minutes=10),
        detail={},
    )
    out1 = await service.raise_signal(project_id, d1, seen_at=now - timedelta(minutes=20))
    out2 = await service.raise_signal(project_id, d2, seen_at=now - timedelta(minutes=10))

    inc1_id = out1.incident_id
    inc2_id = out2.incident_id
    assert inc1_id != inc2_id

    # out1 is older (started_at earlier) -> winner
    merged_winner = await service.merge(project_id, [inc1_id, inc2_id], agent="lead_agent")
    assert merged_winner.id == inc1_id

    # Other incident is merged
    inc2 = await service.get(project_id, inc2_id)
    assert inc2 is not None
    assert inc2.status == "merged"
    assert inc2.merged_into == inc1_id

    # Signals all belong to winner
    sigs_winner = await service.signals(project_id, inc1_id)
    assert len(sigs_winner) == 2
    sigs_other = await service.signals(project_id, inc2_id)
    assert len(sigs_other) == 0

    # Events recorded on both
    ev1 = await event_log.page(project_id, subject_id=inc1_id)
    assert any(e.type == "incident.merged" for e in ev1)
    ev2 = await event_log.page(project_id, subject_id=inc2_id)
    assert any(e.type == "incident.merged" for e in ev2)


@pytest.mark.asyncio
async def test_split_incident(
    service: IncidentsService,
    event_log: SqliteEventLog,
    project_id: str,
    fake_clock: FakeClock,
) -> None:
    now = fake_clock.now()
    d1 = SignalDraft(
        kind="fired_result",
        category="drift",
        subject="feature_a",
        model="m1",
        ran_at=now - timedelta(minutes=15),
        detail={},
    )
    d2 = SignalDraft(
        kind="fired_result",
        category="drift",
        subject="feature_a",
        model="m1",
        ran_at=now - timedelta(minutes=5),
        detail={},
    )
    out1 = await service.raise_signal(project_id, d1, seen_at=now - timedelta(minutes=15))
    out2 = await service.raise_signal(project_id, d2, seen_at=now - timedelta(minutes=5))
    inc_id = out1.incident_id
    assert out2.incident_id == inc_id

    # Split signal 2 into new incident
    new_inc = await service.split(
        project_id, inc_id, signal_ids=[out2.signal_id], agent="split_agent"
    )
    assert new_inc.id != inc_id

    # Original incident has only signal 1
    orig_signals = await service.signals(project_id, inc_id)
    assert len(orig_signals) == 1
    assert orig_signals[0].id == out1.signal_id

    # New incident has signal 2
    new_signals = await service.signals(project_id, new_inc.id)
    assert len(new_signals) == 1
    assert new_signals[0].id == out2.signal_id

    # Events recorded on both
    ev_orig = await event_log.page(project_id, subject_id=inc_id)
    assert any(e.type == "incident.split" for e in ev_orig)
    ev_new = await event_log.page(project_id, subject_id=new_inc.id)
    assert any(e.type == "incident.split" for e in ev_new)


async def _incident_in(
    service: IncidentsService, project_id: str, fake_clock: FakeClock, status: str
) -> str:
    """An incident moved to `status` through the service; returns its id."""
    now = fake_clock.now()

    def draft(subject: str, minutes: int) -> SignalDraft:
        return SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject=subject,
            model=None,
            ran_at=now - timedelta(minutes=minutes),
            detail={},
        )

    if status == "merged":
        winner = await service.raise_signal(project_id, draft(f"{status}_w", 20), seen_at=now)
        loser = await service.raise_signal(project_id, draft(f"{status}_l", 10), seen_at=now)
        await service.merge(project_id, [winner.incident_id, loser.incident_id], agent="a")
        return loser.incident_id
    out = await service.raise_signal(project_id, draft(status, 10), seen_at=now)
    if status == "resolved":
        await service.update(project_id, out.incident_id, "a", IncidentPatch(status="resolved"))
    elif status == "closed":
        await service.close(project_id, out.incident_id)
    return out.incident_id


async def _assert_refused_unchanged(
    service: IncidentsService,
    event_log: SqliteEventLog,
    project_id: str,
    incident_id: str,
    call: Callable[[], Awaitable[object]],
) -> None:
    before = await service.get(project_id, incident_id)
    events_before = await event_log.page(project_id, subject_id=incident_id)
    with pytest.raises(DomainError) as exc_info:
        await call()
    assert exc_info.value.status == 409
    assert exc_info.value.code == "incident_not_live"
    assert await service.get(project_id, incident_id) == before
    assert await event_log.page(project_id, subject_id=incident_id) == events_before


@pytest.mark.parametrize("status", ["closed", "resolved", "merged"])
async def test_acknowledge_refused_when_not_live(
    service: IncidentsService,
    event_log: SqliteEventLog,
    project_id: str,
    fake_clock: FakeClock,
    status: str,
) -> None:
    inc_id = await _incident_in(service, project_id, fake_clock, status)
    await _assert_refused_unchanged(
        service, event_log, project_id, inc_id, lambda: service.acknowledge(project_id, inc_id)
    )


@pytest.mark.parametrize("status", ["closed", "merged"])
async def test_close_refused_when_closed_or_merged(
    service: IncidentsService,
    event_log: SqliteEventLog,
    project_id: str,
    fake_clock: FakeClock,
    status: str,
) -> None:
    inc_id = await _incident_in(service, project_id, fake_clock, status)
    await _assert_refused_unchanged(
        service, event_log, project_id, inc_id, lambda: service.close(project_id, inc_id)
    )


async def test_close_allowed_on_resolved(
    service: IncidentsService, project_id: str, fake_clock: FakeClock
) -> None:
    inc_id = await _incident_in(service, project_id, fake_clock, "resolved")
    inc = await service.close(project_id, inc_id)
    assert inc.status == "closed"


@pytest.mark.parametrize("status", ["closed", "merged"])
@pytest.mark.parametrize("new_status", ["mitigated", "resolved"])
async def test_update_status_refused_when_closed_or_merged(
    service: IncidentsService,
    event_log: SqliteEventLog,
    project_id: str,
    fake_clock: FakeClock,
    status: str,
    new_status: Literal["mitigated", "resolved"],
) -> None:
    inc_id = await _incident_in(service, project_id, fake_clock, status)
    await _assert_refused_unchanged(
        service,
        event_log,
        project_id,
        inc_id,
        lambda: service.update(project_id, inc_id, "a", IncidentPatch(status=new_status)),
    )


@pytest.mark.parametrize("status", ["closed", "merged"])
async def test_merge_refused_when_any_incident_closed_or_merged(
    service: IncidentsService,
    event_log: SqliteEventLog,
    project_id: str,
    fake_clock: FakeClock,
    status: str,
) -> None:
    dead_id = await _incident_in(service, project_id, fake_clock, status)
    live = await service.raise_signal(
        project_id,
        SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="live",
            model=None,
            ran_at=fake_clock.now(),
            detail={},
        ),
        seen_at=fake_clock.now(),
    )
    live_before = await service.get(project_id, live.incident_id)
    live_events_before = await event_log.page(project_id, subject_id=live.incident_id)
    await _assert_refused_unchanged(
        service,
        event_log,
        project_id,
        dead_id,
        lambda: service.merge(project_id, [live.incident_id, dead_id], agent="a"),
    )
    assert await service.get(project_id, live.incident_id) == live_before
    assert await event_log.page(project_id, subject_id=live.incident_id) == live_events_before


@pytest.mark.asyncio
async def test_update_resolved_refused_when_second_subject_still_fires(
    service: IncidentsService,
    fake_results: FakeResults,
    project_id: str,
    fake_clock: FakeClock,
) -> None:
    """An incident with two subjects does not resolve while the second still fires."""
    now = fake_clock.now()
    d1 = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="feature_a",
        model="m1",
        ran_at=now - timedelta(minutes=15),
        detail={},
    )
    d2 = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="feature_b",
        model="m1",
        ran_at=now - timedelta(minutes=10),
        detail={},
    )
    out1 = await service.raise_signal(project_id, d1, seen_at=now - timedelta(minutes=15))
    inc_id = out1.incident_id

    out2 = await service.raise_signal(project_id, d2, seen_at=now - timedelta(minutes=10))
    await service.merge(project_id, [inc_id, out2.incident_id], agent="merger")

    signals = await service.signals(project_id, inc_id)
    assert len(signals) == 2
    assert {s.subject for s in signals} == {"feature_a", "feature_b"}

    # feature_a is cleared, but feature_b is still firing
    fake_results.latest_results = [
        make_stored_result("res_a", fired=False, subject="feature_a", model="m1"),
        make_stored_result("res_b", fired=True, subject="feature_b", model="m1"),
    ]

    with pytest.raises(DomainError) as exc_info:
        await service.update(
            project_id,
            inc_id,
            agent="agent_1",
            patch=IncidentPatch(status="resolved"),
        )
    assert exc_info.value.status == 409
    assert exc_info.value.code == "incident_still_firing"

    # Now feature_b also clears
    fake_results.latest_results = [
        make_stored_result("res_a", fired=False, subject="feature_a", model="m1"),
        make_stored_result("res_b", fired=False, subject="feature_b", model="m1"),
    ]
    fake_clock.advance(30)
    resolved = await service.update(
        project_id,
        inc_id,
        agent="agent_1",
        patch=IncidentPatch(status="resolved"),
    )
    assert resolved.status == "resolved"


@pytest.mark.asyncio
async def test_merge_critical_into_medium_gives_critical_winner_and_reassigns_notifications(
    service: IncidentsService,
    event_log: SqliteEventLog,
    databases: Databases,
    project_id: str,
    fake_clock: FakeClock,
) -> None:
    """Merging a critical incident into a medium incident gives a critical winner and a severity change event."""
    obs = RecordingObserver()
    service.add_observer(obs)

    now = fake_clock.now()
    d1 = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="feature_med",
        model=None,
        ran_at=now - timedelta(minutes=20),
        detail={},
    )
    d2 = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="feature_crit",
        model=None,
        ran_at=now - timedelta(minutes=10),
        detail={},
    )
    out1 = await service.raise_signal(project_id, d1, seen_at=now - timedelta(minutes=20))
    out2 = await service.raise_signal(project_id, d2, seen_at=now - timedelta(minutes=10))
    inc1_id = out1.incident_id
    inc2_id = out2.incident_id

    # Raise inc2 to critical
    await service.set_severity(project_id, inc2_id, "critical", "urgent issue", by="tester")
    inc1_view = await service.get(project_id, inc1_id)
    inc2_view = await service.get(project_id, inc2_id)
    assert inc1_view is not None and inc1_view.severity == "medium"
    assert inc2_view is not None and inc2_view.severity == "critical"

    # Insert a dummy notification_log row for inc2 to verify reassign to winner
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            notification_log.insert().values(
                id="notif_crit_1",
                incident=inc2_id,
                person="alice",
                channel="discord",
                kind="incident",
                escalation_step=0,
                status="sent",
                attempts=1,
                message="Critical incident",
                created=now.isoformat(),
            )
        )

    # Merge inc1 and inc2; inc1 is older, so it is the winner
    obs.changes.clear()
    winner = await service.merge(project_id, [inc1_id, inc2_id], agent="merger")
    assert winner.id == inc1_id
    assert winner.severity == "critical"

    # Verify event recorded
    events = await event_log.page(project_id, subject_id=inc1_id)
    sev_events = [e for e in events if e.type == "incident.severity_changed"]
    assert any(
        e.payload.get("before") == "medium" and e.payload.get("after") == "critical"
        for e in sev_events
    )

    # Verify observer was notified of severity_raised
    raised_changes = [
        c for c in obs.changes if c.kind == "severity_raised" and c.incident_id == inc1_id
    ]
    assert len(raised_changes) == 1
    assert raised_changes[0].before == "medium"
    assert raised_changes[0].after == "critical"

    # Verify notification_log row was reassigned to winner
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    select(notification_log).where(notification_log.c.id == "notif_crit_1")
                )
            )
            .mappings()
            .first()
        )
        assert row is not None
        assert row["incident"] == inc1_id
