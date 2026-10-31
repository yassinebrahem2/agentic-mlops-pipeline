"""The baseline reads Stats uses: `matched_incidents` and `judged_silent_alerts`, on read."""

from __future__ import annotations

from datetime import timedelta

from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.monitoring.verdicts.baseline import SILENT_ALERTS_MAX, SqliteBaseline
from tests.stats.conftest import SOURCE, ago, raise_on, result_record, team_fire

T = ago(days=2)


async def test_a_fire_in_the_part_of_a_window_an_attach_widened_matches_the_incident(
    baseline: SqliteBaseline, incidents: IncidentsService, results: ResultsService, project_id: str
) -> None:
    incident_id = await raise_on(incidents, project_id, subject="churn_scores", started_at=T)
    # 90 minutes before the start is outside the 60-minute grouping window.
    await team_fire(results, project_id, T - timedelta(minutes=90), subject="churn_scores")
    assert await baseline.matched_incidents(project_id) == set()

    # An attach moves started_at 40 minutes earlier; nothing re-links the stored fire.
    attached = await raise_on(
        incidents, project_id, subject="churn_scores", started_at=T - timedelta(minutes=40)
    )
    assert attached == incident_id

    assert await baseline.matched_incidents(project_id) == {incident_id}
    assert await baseline.links(project_id, incident_id) == []


async def test_a_fire_on_a_merged_incidents_key_matches_its_survivor(
    baseline: SqliteBaseline, incidents: IncidentsService, results: ResultsService, project_id: str
) -> None:
    survivor_id = await raise_on(incidents, project_id, subject="features", started_at=T)
    merged_id = await raise_on(
        incidents, project_id, subject="churn_scores", started_at=T + timedelta(minutes=30)
    )
    await incidents.merge(project_id, [survivor_id, merged_id], agent="orchestrator")

    await team_fire(results, project_id, T + timedelta(minutes=40), subject="churn_scores")

    assert await baseline.matched_incidents(project_id) == {survivor_id}


async def test_judged_silent_alerts_are_uncapped_and_carry_their_verdict(
    baseline: SqliteBaseline, results: ResultsService, project_id: str
) -> None:
    old = await team_fire(results, project_id, ago(days=20), subject="churn_scores")
    unjudged = await team_fire(results, project_id, ago(days=19), subject="churn_scores")
    await baseline.judge_silent(project_id, old.id, "not_an_issue", by="user")
    newer = [
        (f"n{n}", result_record(ago(hours=1, seconds=n), subject="churn_scores"))
        for n in range(SILENT_ALERTS_MAX + 1)
    ]
    await results.ingest(project_id, SOURCE, newer, ago(hours=1))

    listed = {a.result_id for a in await baseline.silent_alerts(project_id)}
    [judged] = await baseline.judged_silent_alerts(project_id)

    assert old.id not in listed
    assert judged.result_id == old.id
    assert judged.verdict == "not_an_issue"
    assert judged.ran_at == old.ran_at
    assert unjudged.id not in listed


async def test_a_judged_alert_a_widened_window_now_matches_is_left_out(
    baseline: SqliteBaseline, incidents: IncidentsService, results: ResultsService, project_id: str
) -> None:
    await raise_on(incidents, project_id, subject="churn_scores", started_at=T)
    fire = await team_fire(results, project_id, T - timedelta(minutes=90), subject="churn_scores")
    await baseline.judge_silent(project_id, fire.id, "not_an_issue", by="user")
    assert [a.result_id for a in await baseline.judged_silent_alerts(project_id)] == [fire.id]

    await raise_on(
        incidents, project_id, subject="churn_scores", started_at=T - timedelta(minutes=40)
    )

    assert await baseline.judged_silent_alerts(project_id) == []
