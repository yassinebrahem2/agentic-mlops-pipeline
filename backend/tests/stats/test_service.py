"""StatsService: windows, merged incidents, the baseline, collecting, and the reads it pages."""

from __future__ import annotations

from datetime import timedelta

import pytest

from mlops_agents.monitoring.incidents.contract import IncidentPatch
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.monitoring.verdicts.baseline import SILENT_ALERTS_MAX, SqliteBaseline
from mlops_agents.monitoring.verdicts.contract import VerdictDraft
from mlops_agents.monitoring.verdicts.store import SqliteVerdicts
from mlops_agents.stats.contract import STATS_MIN_VERDICTS, StatsView, StatValue
from mlops_agents.stats.service import StatsService
from mlops_agents.storage.databases import Databases
from tests.providers.fake_clock import FakeClock
from tests.stats.conftest import (
    NOW,
    SOURCE,
    add_approvals,
    add_hook_runs,
    ago,
    raise_on,
    result_record,
    store,
    team_fire,
)

T = ago(days=2)


def _stat(view: StatsView, name: str) -> StatValue:
    return next(group[name] for group in view.groups.values() if name in group)


async def _real_issue(
    incidents: IncidentsService, verdicts: SqliteVerdicts, project_id: str, subject: str
) -> str:
    incident_id = await raise_on(incidents, project_id, subject=subject, started_at=T)
    await verdicts.record(project_id, incident_id, VerdictDraft(value="real_issue"), by="user")
    return incident_id


async def test_collecting_holds_below_the_minimum_verdicts_only_for_verdict_numbers(
    stats: StatsService,
    incidents: IncidentsService,
    verdicts: SqliteVerdicts,
    databases: Databases,
    project_id: str,
) -> None:
    await add_approvals(databases, project_id, [("approved", ago(days=1))])
    for n in range(STATS_MIN_VERDICTS - 1):
        await _real_issue(incidents, verdicts, project_id, f"s{n}")

    view = await stats.compute(project_id, "30d", None, NOW)
    assert _stat(view, "precision").n == STATS_MIN_VERDICTS - 1
    assert _stat(view, "precision").collecting
    assert _stat(view, "precision").value == 1.0
    assert not _stat(view, "approvals_requested").collecting
    assert not _stat(view, "alerts_per_week").collecting

    await _real_issue(incidents, verdicts, project_id, "last")

    view = await stats.compute(project_id, "30d", None, NOW)
    assert _stat(view, "precision").n == STATS_MIN_VERDICTS
    assert not _stat(view, "precision").collecting


async def test_without_team_records_the_baseline_numbers_are_none(
    stats: StatsService, incidents: IncidentsService, verdicts: SqliteVerdicts, project_id: str
) -> None:
    await _real_issue(incidents, verdicts, project_id, "s1")

    view = await stats.compute(project_id, "30d", None, NOW)

    assert not view.baseline_available
    assert _stat(view, "caught_baseline_missed") == StatValue(value=None, n=1, collecting=True)
    assert _stat(view, "false_alarms_avoided") == StatValue(value=None, n=0, collecting=True)
    assert _stat(view, "precision").value == 1.0


async def test_an_incident_is_windowed_by_when_it_was_detected_not_when_it_started(
    stats: StatsService, incidents: IncidentsService, project_id: str
) -> None:
    await raise_on(
        incidents, project_id, subject="late", started_at=ago(days=8), detected_at=ago(days=6)
    )

    view = await stats.compute(project_id, "7d", None, NOW)

    assert _stat(view, "alerts_per_week").n == 1


async def test_an_incident_live_again_with_an_old_resolved_at_is_not_resolved(
    stats: StatsService,
    incidents: IncidentsService,
    verdicts: SqliteVerdicts,
    results: ResultsService,
    clock: FakeClock,
    project_id: str,
) -> None:
    incident_id = await raise_on(incidents, project_id, subject="churn_scores", started_at=T)
    for ran_at, value in ((T - timedelta(hours=1), 0.70), (T + timedelta(hours=3), 0.80)):
        record = result_record(
            ran_at, subject="churn_scores", origin="agent", fired=False, value=value
        )
        await store(results, project_id, record)
    clock.current = T + timedelta(hours=1)
    await incidents.update(
        project_id, incident_id, "orchestrator", IncidentPatch(status="resolved")
    )
    clock.current = T + timedelta(hours=2)
    live = await incidents.update(
        project_id, incident_id, "orchestrator", IncidentPatch(status="mitigated")
    )
    clock.current = NOW
    assert live.resolved_at is not None
    await verdicts.record(project_id, incident_id, VerdictDraft(value="real_issue"), by="user")

    view = await stats.compute(project_id, "30d", None, NOW)

    assert _stat(view, "time_to_resolution_s").n == 0
    assert _stat(view, "metric_before").n == 0
    assert _stat(view, "metric_after").n == 0


async def test_7d_leaves_out_an_8_day_old_incident_and_all_counts_it(
    stats: StatsService, incidents: IncidentsService, project_id: str
) -> None:
    await raise_on(incidents, project_id, subject="old", started_at=ago(days=8))

    week = await stats.compute(project_id, "7d", None, NOW)
    every = await stats.compute(project_id, "all", None, NOW)

    assert _stat(week, "alerts_per_week") == StatValue(value=None, n=0, collecting=False)
    assert _stat(week, "cost_per_incident") == StatValue(value=None, n=0, collecting=False)
    # `all` spreads one incident over the 8 days since the earliest one.
    assert _stat(every, "alerts_per_week").n == 1
    assert _stat(every, "alerts_per_week").value == pytest.approx(7 / 8)
    # Incidents but no model calls: a real zero spend.
    assert _stat(every, "cost_per_incident") == StatValue(value=0.0, n=1, collecting=False)


async def test_a_merged_incident_is_not_counted(
    stats: StatsService, incidents: IncidentsService, project_id: str
) -> None:
    first = await raise_on(incidents, project_id, subject="a", started_at=T)
    second = await raise_on(incidents, project_id, subject="b", started_at=T + timedelta(hours=1))
    await incidents.merge(project_id, [first, second], agent="orchestrator")

    view = await stats.compute(project_id, "30d", None, NOW)

    assert _stat(view, "alerts_per_week").n == 1
    assert _stat(view, "signals_per_incident") == StatValue(value=2.0, n=1, collecting=False)


async def test_a_fire_in_a_window_an_attach_widened_keeps_the_incident_out_of_caught(
    stats: StatsService,
    incidents: IncidentsService,
    verdicts: SqliteVerdicts,
    results: ResultsService,
    project_id: str,
) -> None:
    await _real_issue(incidents, verdicts, project_id, "churn_scores")
    await team_fire(results, project_id, T - timedelta(minutes=90), subject="churn_scores")
    before = await stats.compute(project_id, "30d", None, NOW)
    assert _stat(before, "caught_baseline_missed").value == 1

    await raise_on(
        incidents, project_id, subject="churn_scores", started_at=T - timedelta(minutes=40)
    )

    after = await stats.compute(project_id, "30d", None, NOW)
    assert _stat(after, "caught_baseline_missed") == StatValue(value=0, n=1, collecting=True)


async def test_a_fire_on_a_merged_incidents_key_keeps_its_survivor_out_of_caught(
    stats: StatsService,
    incidents: IncidentsService,
    verdicts: SqliteVerdicts,
    results: ResultsService,
    project_id: str,
) -> None:
    survivor = await _real_issue(incidents, verdicts, project_id, "features")
    merged = await raise_on(
        incidents, project_id, subject="churn_scores", started_at=T + timedelta(minutes=30)
    )
    await incidents.merge(project_id, [survivor, merged], agent="orchestrator")
    # A team record so the baseline is available.
    await team_fire(results, project_id, ago(days=20), subject="elsewhere")
    assert (
        _stat(await stats.compute(project_id, "30d", None, NOW), "caught_baseline_missed").value
        == 1
    )

    await team_fire(results, project_id, T + timedelta(minutes=40), subject="churn_scores")

    view = await stats.compute(project_id, "30d", None, NOW)
    assert _stat(view, "caught_baseline_missed") == StatValue(value=0, n=1, collecting=True)


async def test_past_500_silent_alerts_an_old_judged_one_still_counts_in_its_window(
    stats: StatsService, baseline: SqliteBaseline, results: ResultsService, project_id: str
) -> None:
    old = await team_fire(results, project_id, ago(days=20), subject="churn_scores")
    await baseline.judge_silent(project_id, old.id, "not_an_issue", by="user")
    newer = [
        (f"n{n}", result_record(ago(hours=1, seconds=n), subject="churn_scores"))
        for n in range(SILENT_ALERTS_MAX + 1)
    ]
    await results.ingest(project_id, SOURCE, newer, ago(hours=1))

    month = await stats.compute(project_id, "30d", None, NOW)
    week = await stats.compute(project_id, "7d", None, NOW)

    assert _stat(month, "false_alarms_avoided").value == 1
    assert _stat(month, "false_alarms_avoided").n == 1
    assert _stat(week, "false_alarms_avoided").n == 0


async def test_a_judged_alert_a_widened_window_now_matches_is_no_longer_counted(
    stats: StatsService,
    baseline: SqliteBaseline,
    incidents: IncidentsService,
    results: ResultsService,
    project_id: str,
) -> None:
    await raise_on(incidents, project_id, subject="churn_scores", started_at=T)
    fire = await team_fire(results, project_id, T - timedelta(minutes=90), subject="churn_scores")
    await baseline.judge_silent(project_id, fire.id, "real_issue", by="user")
    assert _stat(await stats.compute(project_id, "30d", None, NOW), "misses").value == 1

    await raise_on(
        incidents, project_id, subject="churn_scores", started_at=T - timedelta(minutes=40)
    )

    view = await stats.compute(project_id, "30d", None, NOW)
    assert _stat(view, "misses") == StatValue(value=None, n=0, collecting=False)
    assert _stat(view, "false_alarms_avoided").n == 0


async def test_every_hook_run_in_the_window_counts_past_the_first_read(
    stats: StatsService, databases: Databases, project_id: str
) -> None:
    runs = [("command", "rule", ago(hours=1, seconds=n)) for n in range(250)]
    runs += [("command", "approval", ago(hours=2, seconds=n)) for n in range(250)]
    await add_hook_runs(databases, project_id, runs)

    view = await stats.compute(project_id, "30d", None, NOW)

    assert _stat(view, "autonomous_share") == StatValue(value=0.5, n=500, collecting=False)
