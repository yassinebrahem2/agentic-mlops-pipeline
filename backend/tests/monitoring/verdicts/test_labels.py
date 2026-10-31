"""Confirmation by labels: label-based checks confirm or refute model-performance incidents."""

from __future__ import annotations

from datetime import timedelta

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.monitoring.results.contract import ResultRecord
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.monitoring.verdicts.contract import VerdictDraft
from mlops_agents.monitoring.verdicts.labels import LabelConfirmation
from mlops_agents.monitoring.verdicts.store import SqliteVerdicts
from tests.monitoring.verdicts.conftest import (
    SOURCE,
    T0,
    open_incident,
    store_result,
    verdict_events,
)
from tests.providers.fake_clock import FakeClock

LATER = T0 + timedelta(hours=1)


async def test_fired_label_results_confirm_a_real_issue(
    verdicts: SqliteVerdicts,
    incidents: IncidentsService,
    results: ResultsService,
    events: SqliteEventLog,
    project_id: str,
    clock: FakeClock,
) -> None:
    incident_id = await open_incident(incidents, project_id)
    await store_result(
        results, project_id, check_id="chk_auc", ran_at=LATER, fired=True, version="v3"
    )
    await store_result(results, project_id, check_id="chk_recall", ran_at=LATER, fired=False)

    recorded = await verdicts.confirm_by_labels(project_id)

    assert len(recorded) == 1
    view = recorded[0]
    assert view.incident_id == incident_id
    assert view.value == "real_issue"
    assert view.source == "labels"
    assert view.check_versions == {"chk_auc": "v3"}
    assert view.created == clock.now()
    assert await verdicts.get(project_id, incident_id) == view
    written = await verdict_events(events, project_id)
    assert len(written) == 1
    assert written[0].source == "labels"
    assert written[0].subject_id == incident_id
    assert written[0].payload == {"value": "real_issue", "source": "labels"}


async def test_the_latest_valid_result_per_check_decides_and_none_fired_refutes(
    verdicts: SqliteVerdicts,
    incidents: IncidentsService,
    results: ResultsService,
    project_id: str,
) -> None:
    incident_id = await open_incident(incidents, project_id)
    # An older fired result is superseded by a newer quiet one of the same check.
    await store_result(results, project_id, check_id="chk_auc", ran_at=LATER, fired=True)
    await store_result(
        results, project_id, check_id="chk_auc", ran_at=LATER + timedelta(hours=1), fired=False
    )
    # A newer not-enough-data record is the minimum sample not being met: it never decides.
    await store_result(
        results,
        project_id,
        check_id="chk_auc",
        ran_at=LATER + timedelta(hours=2),
        fired=True,
        not_enough_data=True,
    )
    # None of these is evidence: at the incident's start, from the team, another category.
    await store_result(results, project_id, check_id="chk_start", ran_at=T0, fired=True)
    await store_result(
        results, project_id, check_id="chk_team", ran_at=LATER, fired=True, origin="team"
    )
    await store_result(
        results, project_id, check_id="chk_nulls", ran_at=LATER, fired=True, category="drift"
    )
    await store_result(
        results, project_id, check_id="chk_fraud", ran_at=LATER, fired=True, model="fraud"
    )

    recorded = await verdicts.confirm_by_labels(project_id)

    assert [(v.incident_id, v.value) for v in recorded] == [(incident_id, "not_an_issue")]


async def test_only_not_enough_data_records_decide_nothing(
    verdicts: SqliteVerdicts,
    incidents: IncidentsService,
    results: ResultsService,
    events: SqliteEventLog,
    project_id: str,
) -> None:
    incident_id = await open_incident(incidents, project_id)
    await store_result(
        results, project_id, check_id="chk_auc", ran_at=LATER, fired=True, not_enough_data=True
    )
    await store_result(
        results, project_id, check_id="chk_recall", ran_at=LATER, fired=False, not_enough_data=True
    )

    assert await verdicts.confirm_by_labels(project_id) == []
    assert await verdicts.get(project_id, incident_id) is None
    assert await verdict_events(events, project_id) == []


async def test_a_user_verdict_is_never_replaced(
    verdicts: SqliteVerdicts,
    incidents: IncidentsService,
    results: ResultsService,
    project_id: str,
) -> None:
    incident_id = await open_incident(incidents, project_id)
    user_view = await verdicts.record(
        project_id, incident_id, VerdictDraft(value="not_an_issue"), by="user"
    )
    await store_result(results, project_id, check_id="chk_auc", ran_at=LATER, fired=True)

    assert await verdicts.confirm_by_labels(project_id) == []
    assert await verdicts.get(project_id, incident_id) == user_view


async def test_a_user_verdict_replaces_a_labels_verdict(
    verdicts: SqliteVerdicts,
    incidents: IncidentsService,
    results: ResultsService,
    project_id: str,
) -> None:
    incident_id = await open_incident(incidents, project_id)
    await store_result(results, project_id, check_id="chk_auc", ran_at=LATER, fired=True)
    await verdicts.confirm_by_labels(project_id)

    user_view = await verdicts.record(
        project_id, incident_id, VerdictDraft(value="not_an_issue"), by="user"
    )

    assert await verdicts.get(project_id, incident_id) == user_view
    assert await verdicts.confirm_by_labels(project_id) == []
    assert await verdicts.get(project_id, incident_id) == user_view


async def test_a_second_sync_with_no_new_results_writes_no_event_and_a_change_replaces(
    verdicts: SqliteVerdicts,
    incidents: IncidentsService,
    results: ResultsService,
    events: SqliteEventLog,
    project_id: str,
) -> None:
    incident_id = await open_incident(incidents, project_id)
    await store_result(results, project_id, check_id="chk_auc", ran_at=LATER, fired=True)
    confirmation = LabelConfirmation(verdicts)

    await confirmation.synced(project_id, LATER)
    await confirmation.synced(project_id, LATER)

    assert len(await verdict_events(events, project_id)) == 1
    assert await verdicts.confirm_by_labels(project_id) == []

    await store_result(
        results, project_id, check_id="chk_auc", ran_at=LATER + timedelta(hours=1), fired=False
    )
    changed = await verdicts.confirm_by_labels(project_id)

    assert [(v.incident_id, v.value) for v in changed] == [(incident_id, "not_an_issue")]
    stored = await verdicts.get(project_id, incident_id)
    assert stored is not None
    assert stored.value == "not_an_issue"
    assert stored.source == "labels"
    assert len(await verdict_events(events, project_id)) == 2


async def test_non_model_performance_and_finished_incidents_are_untouched(
    verdicts: SqliteVerdicts,
    incidents: IncidentsService,
    results: ResultsService,
    project_id: str,
) -> None:
    drift = await open_incident(incidents, project_id, subject="features", category="drift")
    no_model = await open_incident(incidents, project_id, subject="scores", model=None)
    closed = await open_incident(incidents, project_id, subject="old_scores")
    await incidents.close(project_id, closed, by="user")
    await store_result(results, project_id, check_id="chk_auc", ran_at=LATER, fired=True)
    await store_result(
        results, project_id, check_id="chk_auc_none", ran_at=LATER, fired=True, model=None
    )

    assert await verdicts.confirm_by_labels(project_id) == []
    for incident_id in (drift, no_model, closed):
        assert await verdicts.get(project_id, incident_id) is None


async def test_a_labels_verdict_never_closes(
    verdicts: SqliteVerdicts,
    incidents: IncidentsService,
    results: ResultsService,
    project_id: str,
) -> None:
    incident_id = await open_incident(incidents, project_id, resolved=True)
    await store_result(results, project_id, check_id="chk_auc", ran_at=LATER, fired=False)

    recorded = await verdicts.confirm_by_labels(project_id)

    assert [v.value for v in recorded] == ["not_an_issue"]
    incident = await incidents.get(project_id, incident_id)
    assert incident is not None
    assert incident.status == "resolved"


async def test_evidence_past_one_page_of_results_is_read(
    verdicts: SqliteVerdicts,
    incidents: IncidentsService,
    results: ResultsService,
    project_id: str,
) -> None:
    incident_id = await open_incident(incidents, project_id)
    await store_result(results, project_id, check_id="chk_late", ran_at=LATER, fired=True)
    # 500 newer quiet results of another check fill the first page of the newest-first query.
    quiet = [
        (
            f"quiet{i}",
            ResultRecord(
                check_id="chk_busy",
                origin="agent",
                ran_at=LATER + timedelta(minutes=1 + i),
                fired=False,
                category="model_performance",
                subject="churn_scores",
                model="churn",
                detail={},
                source_ref=f"busy:{i}",
            ),
        )
        for i in range(500)
    ]
    await results.ingest(project_id, SOURCE, quiet, LATER)

    recorded = await verdicts.confirm_by_labels(project_id)

    assert [(v.incident_id, v.value) for v in recorded] == [(incident_id, "real_issue")]
