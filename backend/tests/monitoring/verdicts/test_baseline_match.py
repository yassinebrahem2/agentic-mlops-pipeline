"""The one rule that ties a team fire to an incident (Decision #565's window end)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from mlops_agents.monitoring.incidents.contract import IncidentStatus, IncidentView, cause_key
from mlops_agents.monitoring.results.contract import ResultOrigin, StoredResult
from mlops_agents.monitoring.verdicts.baseline import match_window_end, team_fire_matches

T0 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
NOW = T0 + timedelta(hours=6)
WINDOW = 60
SUBJECT = "churn_scores"
MODEL = "churn"


def _incident(
    status: IncidentStatus = "open",
    *,
    id: str = "inc_1",
    subject: str = SUBJECT,
    started_at: datetime = T0,
    **fields: Any,
) -> IncidentView:
    return IncidentView(
        id=id,
        category="model_performance",
        severity="medium",
        status=status,
        cause_key=cause_key(subject, MODEL),
        model=MODEL,
        started_at=started_at,
        detected_at=started_at,
        **fields,
    )


def _fire(
    ran_at: datetime,
    *,
    subject: str = SUBJECT,
    origin: ResultOrigin = "team",
    fired: bool = True,
    not_enough_data: bool = False,
) -> StoredResult:
    return StoredResult(
        id="res_1",
        check_id="team_recall",
        origin=origin,
        ran_at=ran_at,
        fired=fired,
        category="model_performance",
        subject=subject,
        model=MODEL,
        detail={},
        source_ref="team.jsonl:1",
        not_enough_data=not_enough_data,
        source_id="file:conn:team.jsonl",
        source_key="k1",
        seen_at=ran_at,
    )


def _matches(
    result: StoredResult, incident: IncidentView, survivor: IncidentView | None = None
) -> bool:
    return team_fire_matches(result, incident, WINDOW, now=NOW, survivor=survivor)


def test_a_live_incident_matches_from_its_start_less_the_window_up_to_now() -> None:
    incident = _incident("acknowledged")
    start = T0 - timedelta(minutes=WINDOW)

    assert _matches(_fire(start), incident)
    assert not _matches(_fire(start - timedelta(seconds=1)), incident)
    assert _matches(_fire(NOW), incident)
    assert not _matches(_fire(NOW + timedelta(seconds=1)), incident)
    assert match_window_end(incident, now=NOW) == NOW


def test_a_resolved_incident_stops_matching_after_resolved_at() -> None:
    resolved_at = T0 + timedelta(hours=1)
    incident = _incident("resolved", resolved_at=resolved_at)

    assert _matches(_fire(resolved_at), incident)
    assert not _matches(_fire(resolved_at + timedelta(seconds=1)), incident)


def test_an_incident_a_person_closed_while_open_stops_matching_after_closed_at() -> None:
    closed_at = T0 + timedelta(hours=2)
    incident = _incident("closed", closed_at=closed_at)

    assert match_window_end(incident, now=NOW) == closed_at
    assert _matches(_fire(closed_at), incident)
    assert not _matches(_fire(closed_at + timedelta(seconds=1)), incident)


def test_a_closed_incident_that_was_resolved_ends_at_resolved_at() -> None:
    resolved_at = T0 + timedelta(hours=1)
    incident = _incident("closed", resolved_at=resolved_at, closed_at=T0 + timedelta(hours=3))

    assert match_window_end(incident, now=NOW) == resolved_at


def test_a_reopened_incident_with_an_old_resolved_at_still_matches_a_fire_now() -> None:
    incident = _incident("reopened", resolved_at=T0 + timedelta(hours=1))

    assert match_window_end(incident, now=NOW) == NOW
    assert _matches(_fire(NOW), incident)


def test_a_merged_incident_matches_its_own_key_up_to_its_survivors_end() -> None:
    survivor = _incident(
        "open", id="inc_survivor", subject="other_table", started_at=T0 - timedelta(hours=3)
    )
    merged = _incident("merged", merged_into=survivor.id)

    assert match_window_end(merged, now=NOW, survivor=survivor) == NOW
    assert _matches(_fire(NOW), merged, survivor)
    # The start stays the merged incident's own, not the survivor's earlier one.
    assert not _matches(_fire(T0 - timedelta(minutes=WINDOW, seconds=1)), merged, survivor)
    # The survivor's own key is not the merged one's.
    assert not _matches(_fire(NOW, subject="other_table"), merged, survivor)


def test_a_merged_incident_ends_where_its_resolved_survivor_ends() -> None:
    resolved_at = T0 + timedelta(hours=2)
    survivor = _incident("resolved", id="inc_survivor", resolved_at=resolved_at)
    merged = _incident("merged", merged_into=survivor.id)

    assert _matches(_fire(resolved_at), merged, survivor)
    assert not _matches(_fire(resolved_at + timedelta(seconds=1)), merged, survivor)


def test_a_merged_incident_without_its_survivor_matches_nothing() -> None:
    merged = _incident("merged", merged_into="inc_gone")

    assert match_window_end(merged, now=NOW) is None
    assert not _matches(_fire(T0), merged)


def test_a_live_incidents_survivor_argument_is_ignored() -> None:
    incident = _incident("open")
    other = _incident("resolved", id="inc_other", resolved_at=T0)

    assert _matches(_fire(NOW), incident, other)


def test_only_a_team_fire_on_the_incidents_cause_key_matches() -> None:
    incident = _incident("open")

    assert _matches(_fire(T0), incident)
    assert not _matches(_fire(T0, subject="other_table"), incident)
    assert not _matches(_fire(T0, origin="agent"), incident)
    assert not _matches(_fire(T0, origin="tracker"), incident)
    assert not _matches(_fire(T0, fired=False), incident)
    assert not _matches(_fire(T0, not_enough_data=True), incident)
