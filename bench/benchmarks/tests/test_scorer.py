from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from demo_world.answer_key import CiFailureEvent, PullDecision, Record
from fastapi import FastAPI
from mlops_agents.monitoring.incidents.contract import SignalDraft
from mlops_agents.monitoring.results.contract import ResultRecord, source_id
from mlops_agents.monitoring.verdicts.contract import VerdictDraft

from benchmarks.client import in_process
from benchmarks.dates import DayStarts
from benchmarks.scorer import (
    GRACE_DAYS,
    AlreadyScored,
    Problem,
    Score,
    problems,
    score,
)

type AppMaker = Callable[[str], AbstractAsyncContextManager[tuple[FastAPI, str, Path]]]


async def _create_project(client: Any, tmp_path: Path, name: str = "test_repo") -> str:
    repo_dir = tmp_path / name
    repo_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "source": "local",
        "path": str(repo_dir),
        "init_git": True,
    }
    project = await client.post("/projects", body)
    return str(project["id"])


def _day_starts(start_date: date, count: int) -> DayStarts:
    return [
        (
            start_date + timedelta(days=i),
            datetime(
                (start_date + timedelta(days=i)).year,
                (start_date + timedelta(days=i)).month,
                (start_date + timedelta(days=i)).day,
                8,
                0,
                0,
                tzinfo=UTC,
            ),
        )
        for i in range(count)
    ]


# -------------------------------------------------------------------------
# Unit tests on `problems` grouping and line filtering
# -------------------------------------------------------------------------


def test_problems_groups_records_by_kind_start_end() -> None:
    rec1 = Record(
        kind="null_spike",
        category="data_quality",
        column="col_a",
        start="2026-07-01",
        end="2026-07-03",
    )
    rec2 = Record(
        kind="null_spike",
        category="data_quality",
        column="col_b",
        start="2026-07-01",
        end="2026-07-03",
    )
    rec3 = Record(
        kind="covariate_shift",
        category="drift",
        column="col_a",
        start="2026-07-15",
        end="2026-07-18",
    )
    key = [rec1, rec2, rec3]
    probs = problems(key)

    assert len(probs) == 2
    assert probs[0] == Problem(
        kind="null_spike",
        category="data_quality",
        start=date(2026, 7, 1),
        end=date(2026, 7, 3),
        columns=("col_a", "col_b"),
    )
    assert probs[1] == Problem(
        kind="covariate_shift",
        category="drift",
        start=date(2026, 7, 15),
        end=date(2026, 7, 18),
        columns=("col_a",),
    )


def test_problems_skips_ci_failure_and_pull_decision() -> None:
    rec = Record(
        kind="null_spike",
        category="data_quality",
        column="col_a",
        start="2026-07-01",
        end="2026-07-03",
    )
    ci_event = CiFailureEvent(
        kind="ci_failure_set",
        start="2026-07-01",
        end="2026-07-03",
        date="2026-07-01",
        time="08:00:00",
        set_at="08:00:00",
    )
    pull_decision = PullDecision(
        kind="pr_not_merged",
        pr=1,
        branch="main",
        opened="2026-07-01",
        date="2026-07-02",
        delay_days=1,
        merge_after_days=3,
        reason="conflict",
    )
    key: list[Any] = [ci_event, rec, pull_decision]
    probs = problems(key)

    assert len(probs) == 1
    assert probs[0].kind == "null_spike"


# -------------------------------------------------------------------------
# Integration tests against in-process app
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_in_problem_window_same_category(
    app_factory: AppMaker, tmp_path: Path
) -> None:
    """An incident in a problem's window of its category -> real_issue."""
    async with app_factory("test-token") as (app, token, _):
        client = in_process(app, token)
        project_id = await _create_project(client, tmp_path)

        starts = _day_starts(date(2026, 7, 1), 10)
        key = [
            Record(
                kind="null_spike",
                category="data_quality",
                column="col_a",
                start="2026-07-03",
                end="2026-07-05",
            )
        ]

        # Incident on 2026-07-04 (business date falls in [2026-07-02, 2026-07-12])
        services = app.state.services
        inc_dt = datetime(2026, 7, 4, 10, 0, 0, tzinfo=UTC)
        draft = SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="col_a",
            model=None,
            ran_at=inc_dt,
            detail={},
            result_id=None,
            cleared=False,
        )
        outcome = await services.incidents.raise_signal(project_id, draft, seen_at=inc_dt)

        res = await score(client, project_id, key, starts)
        assert isinstance(res, Score)

        assert outcome.incident_id in res.matched
        assert res.matched[outcome.incident_id].kind == "null_spike"
        assert res.verdicts[outcome.incident_id] == VerdictDraft(value="real_issue")
        assert res.misses == []

        # Verify posted verdict via GET /verdicts
        stored_verdicts = await client.get(f"/projects/{project_id}/verdicts")
        assert len(stored_verdicts) == 1
        assert stored_verdicts[0]["incident_id"] == outcome.incident_id
        assert stored_verdicts[0]["value"] == "real_issue"


@pytest.mark.asyncio
async def test_incident_in_problem_window_different_category(
    app_factory: AppMaker, tmp_path: Path
) -> None:
    """An incident in a problem's window of another category -> wrong_type."""
    async with app_factory("test-token") as (app, token, _):
        client = in_process(app, token)
        project_id = await _create_project(client, tmp_path)

        starts = _day_starts(date(2026, 7, 1), 10)
        key = [
            Record(
                kind="covariate_shift",
                category="drift",
                column="col_a",
                start="2026-07-03",
                end="2026-07-05",
            )
        ]

        services = app.state.services
        inc_dt = datetime(2026, 7, 4, 10, 0, 0, tzinfo=UTC)
        # Incident category is data_quality, but problem is drift
        draft = SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="col_a",
            model=None,
            ran_at=inc_dt,
            detail={},
            result_id=None,
            cleared=False,
        )
        outcome = await services.incidents.raise_signal(project_id, draft, seen_at=inc_dt)

        res = await score(client, project_id, key, starts)
        assert isinstance(res, Score)

        assert outcome.incident_id in res.matched
        assert res.verdicts[outcome.incident_id] == VerdictDraft(
            value="wrong_type", right_category="drift"
        )
        assert res.misses == []


@pytest.mark.asyncio
async def test_incident_outside_every_window(app_factory: AppMaker, tmp_path: Path) -> None:
    """An incident outside every window -> not_an_issue."""
    async with app_factory("test-token") as (app, token, _):
        client = in_process(app, token)
        project_id = await _create_project(client, tmp_path)

        starts = _day_starts(date(2026, 7, 1), 25)
        # Problem ends on 2026-07-03. Grace days = 7, window ends on 2026-07-10.
        key = [
            Record(
                kind="null_spike",
                category="data_quality",
                column="col_a",
                start="2026-07-01",
                end="2026-07-03",
            )
        ]

        services = app.state.services
        # Incident detected on 2026-07-15 (outside window and grace)
        inc_dt = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
        draft = SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="col_a",
            model=None,
            ran_at=inc_dt,
            detail={},
            result_id=None,
            cleared=False,
        )
        outcome = await services.incidents.raise_signal(project_id, draft, seen_at=inc_dt)

        res = await score(client, project_id, key, starts)
        assert isinstance(res, Score)

        assert outcome.incident_id not in res.matched
        assert res.verdicts[outcome.incident_id] == VerdictDraft(value="not_an_issue")
        # Problem was uncaught, so it is a miss
        assert len(res.misses) == 1
        assert res.misses[0].kind == "null_spike"


@pytest.mark.asyncio
async def test_problem_two_columns_one_incident_no_miss(
    app_factory: AppMaker, tmp_path: Path
) -> None:
    """A problem on two columns caught by one incident -> no miss."""
    async with app_factory("test-token") as (app, token, _):
        client = in_process(app, token)
        project_id = await _create_project(client, tmp_path)

        starts = _day_starts(date(2026, 7, 1), 10)
        # Demo-world writes one Record per column
        key = [
            Record(
                kind="null_spike",
                category="data_quality",
                column="col_a",
                start="2026-07-03",
                end="2026-07-05",
            ),
            Record(
                kind="null_spike",
                category="data_quality",
                column="col_b",
                start="2026-07-03",
                end="2026-07-05",
            ),
        ]

        services = app.state.services
        inc_dt = datetime(2026, 7, 4, 10, 0, 0, tzinfo=UTC)
        draft = SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="col_a",
            model=None,
            ran_at=inc_dt,
            detail={},
            result_id=None,
            cleared=False,
        )
        outcome = await services.incidents.raise_signal(project_id, draft, seen_at=inc_dt)

        res = await score(client, project_id, key, starts)
        assert isinstance(res, Score)

        assert outcome.incident_id in res.matched
        assert res.misses == []

        misses = await client.get(f"/projects/{project_id}/misses")
        assert misses == []


@pytest.mark.asyncio
async def test_uncaught_problem_one_miss_at_start(app_factory: AppMaker, tmp_path: Path) -> None:
    """An uncaught problem -> one miss at its start."""
    async with app_factory("test-token") as (app, token, _):
        client = in_process(app, token)
        project_id = await _create_project(client, tmp_path)

        starts = _day_starts(date(2026, 7, 1), 10)
        key = [
            Record(
                kind="null_spike",
                category="data_quality",
                column="col_a",
                start="2026-07-03",
                end="2026-07-05",
            )
        ]

        # No incidents raised
        res = await score(client, project_id, key, starts)
        assert isinstance(res, Score)

        assert len(res.misses) == 1
        assert res.misses[0].kind == "null_spike"
        assert res.misses[0].start == date(2026, 7, 3)

        misses = await client.get(f"/projects/{project_id}/misses")
        assert len(misses) == 1
        assert misses[0]["category"] == "data_quality"
        # occurred_at must match day start of 2026-07-03: 2026-07-03T08:00:00+00:00
        assert "2026-07-03T08:00:00" in misses[0]["occurred_at"]


@pytest.mark.asyncio
async def test_incident_day_before_problem_start_matches(
    app_factory: AppMaker, tmp_path: Path
) -> None:
    """An incident detected the day before a problem's start matches it."""
    async with app_factory("test-token") as (app, token, _):
        client = in_process(app, token)
        project_id = await _create_project(client, tmp_path)

        starts = _day_starts(date(2026, 7, 1), 10)
        # Problem starts on 2026-07-05
        key = [
            Record(
                kind="null_spike",
                category="data_quality",
                column="col_a",
                start="2026-07-05",
                end="2026-07-07",
            )
        ]

        # Incident detected on 2026-07-04 (day before start)
        services = app.state.services
        inc_dt = datetime(2026, 7, 4, 10, 0, 0, tzinfo=UTC)
        draft = SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="col_a",
            model=None,
            ran_at=inc_dt,
            detail={},
            result_id=None,
            cleared=False,
        )
        outcome = await services.incidents.raise_signal(project_id, draft, seen_at=inc_dt)

        res = await score(client, project_id, key, starts)
        assert isinstance(res, Score)

        assert outcome.incident_id in res.matched
        assert res.verdicts[outcome.incident_id] == VerdictDraft(value="real_issue")
        assert res.misses == []


@pytest.mark.asyncio
async def test_incident_grace_days_plus_one_does_not_match(
    app_factory: AppMaker, tmp_path: Path
) -> None:
    """One detected GRACE_DAYS + 1 after its end does not match."""
    async with app_factory("test-token") as (app, token, _):
        client = in_process(app, token)
        project_id = await _create_project(client, tmp_path)

        starts = _day_starts(date(2026, 7, 1), 20)
        # Problem end: 2026-07-05. GRACE_DAYS = 7 -> grace ends 2026-07-12.
        # GRACE_DAYS + 1 is 2026-07-13.
        key = [
            Record(
                kind="null_spike",
                category="data_quality",
                column="col_a",
                start="2026-07-03",
                end="2026-07-05",
            )
        ]

        services = app.state.services
        inc_dt = datetime(2026, 7, 5 + GRACE_DAYS + 1, 10, 0, 0, tzinfo=UTC)
        draft = SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="col_a",
            model=None,
            ran_at=inc_dt,
            detail={},
            result_id=None,
            cleared=False,
        )
        outcome = await services.incidents.raise_signal(project_id, draft, seen_at=inc_dt)

        res = await score(client, project_id, key, starts)
        assert isinstance(res, Score)

        assert outcome.incident_id not in res.matched
        assert res.verdicts[outcome.incident_id] == VerdictDraft(value="not_an_issue")
        assert len(res.misses) == 1


@pytest.mark.asyncio
async def test_candidate_in_window_beats_grace_days(app_factory: AppMaker, tmp_path: Path) -> None:
    """A date inside one problem's window and in another's grace days goes to the first."""
    async with app_factory("test-token") as (app, token, _):
        client = in_process(app, token)
        project_id = await _create_project(client, tmp_path)

        starts = _day_starts(date(2026, 7, 1), 25)
        # Problem 1: start 2026-07-01, end 2026-07-03. Grace: 2026-07-04 to 2026-07-10.
        # Problem 2: start 2026-07-10, end 2026-07-12. Window [start-1, end]: 2026-07-09 to 2026-07-12.
        # On 2026-07-10:
        # Problem 1 reaches it only via grace days.
        # Problem 2 has it inside its own window [2026-07-09, 2026-07-12].
        # Even if incident has Problem 1's category, Problem 2 must win!
        p1 = Record(
            kind="prob1",
            category="data_quality",
            column="col_a",
            start="2026-07-01",
            end="2026-07-03",
        )
        p2 = Record(
            kind="prob2",
            category="drift",
            column="col_b",
            start="2026-07-10",
            end="2026-07-12",
        )
        key = [p1, p2]

        services = app.state.services
        # Incident on 2026-07-10 with data_quality category
        inc_dt = datetime(2026, 7, 10, 10, 0, 0, tzinfo=UTC)
        draft = SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="col_a",
            model=None,
            ran_at=inc_dt,
            detail={},
            result_id=None,
            cleared=False,
        )
        outcome = await services.incidents.raise_signal(project_id, draft, seen_at=inc_dt)

        res = await score(client, project_id, key, starts)
        assert isinstance(res, Score)

        # Matched problem must be prob2 (in window beats grace days)
        assert outcome.incident_id in res.matched
        assert res.matched[outcome.incident_id].kind == "prob2"
        # Since incident category is data_quality, but prob2 is drift -> wrong_type
        assert res.verdicts[outcome.incident_id] == VerdictDraft(
            value="wrong_type", right_category="drift"
        )
        # prob1 was uncaught, so it is a miss
        assert len(res.misses) == 1
        assert res.misses[0].kind == "prob1"


@pytest.mark.asyncio
async def test_candidate_category_match_beats_earlier_mismatched_problem(
    app_factory: AppMaker, tmp_path: Path
) -> None:
    """A problem whose category matches incident.category beats an earlier problem of a different category."""
    async with app_factory("test-token") as (app, token, _):
        client = in_process(app, token)
        project_id = await _create_project(client, tmp_path)

        starts = _day_starts(date(2026, 7, 1), 20)
        # Problem 1: start 2026-07-02, end 2026-07-05, category "drift" (earlier)
        # Problem 2: start 2026-07-03, end 2026-07-06, category "data_quality" (later)
        p1 = Record(
            kind="covariate_shift",
            category="drift",
            column="col_a",
            start="2026-07-02",
            end="2026-07-05",
        )
        p2 = Record(
            kind="null_spike",
            category="data_quality",
            column="col_b",
            start="2026-07-03",
            end="2026-07-06",
        )
        key = [p1, p2]

        services = app.state.services
        # Incident on 2026-07-04 (in window for both): category "data_quality"
        inc_dt = datetime(2026, 7, 4, 10, 0, 0, tzinfo=UTC)
        draft = SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="col_b",
            model=None,
            ran_at=inc_dt,
            detail={},
            result_id=None,
            cleared=False,
        )
        outcome = await services.incidents.raise_signal(project_id, draft, seen_at=inc_dt)

        res = await score(client, project_id, key, starts)
        assert isinstance(res, Score)

        # p2 matches category -> must beat p1 despite p1 having earlier start date
        assert outcome.incident_id in res.matched
        assert res.matched[outcome.incident_id].kind == "null_spike"
        assert res.verdicts[outcome.incident_id] == VerdictDraft(value="real_issue")
        # p1 was uncaught -> miss
        assert len(res.misses) == 1
        assert res.misses[0].kind == "covariate_shift"


@pytest.mark.asyncio
async def test_candidate_earlier_problem_beats_later_problem_same_category(
    app_factory: AppMaker, tmp_path: Path
) -> None:
    """An earlier problem beats a later problem when both match category and window tier."""
    async with app_factory("test-token") as (app, token, _):
        client = in_process(app, token)
        project_id = await _create_project(client, tmp_path)

        starts = _day_starts(date(2026, 7, 1), 20)
        # Problem 1: start 2026-07-01, end 2026-07-04, category "data_quality" (earlier)
        # Problem 2: start 2026-07-03, end 2026-07-06, category "data_quality" (later)
        p1 = Record(
            kind="null_spike_1",
            category="data_quality",
            column="col_a",
            start="2026-07-01",
            end="2026-07-04",
        )
        p2 = Record(
            kind="null_spike_2",
            category="data_quality",
            column="col_b",
            start="2026-07-03",
            end="2026-07-06",
        )
        key = [p1, p2]

        services = app.state.services
        # Incident on 2026-07-03 (in window for both): category "data_quality"
        inc_dt = datetime(2026, 7, 3, 10, 0, 0, tzinfo=UTC)
        draft = SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="col_a",
            model=None,
            ran_at=inc_dt,
            detail={},
            result_id=None,
            cleared=False,
        )
        outcome = await services.incidents.raise_signal(project_id, draft, seen_at=inc_dt)

        res = await score(client, project_id, key, starts)
        assert isinstance(res, Score)

        # Both match category and window tier; earlier problem p1 wins
        assert outcome.incident_id in res.matched
        assert res.matched[outcome.incident_id].kind == "null_spike_1"
        assert res.verdicts[outcome.incident_id] == VerdictDraft(value="real_issue")
        # p2 was uncaught -> miss
        assert len(res.misses) == 1
        assert res.misses[0].kind == "null_spike_2"


@pytest.mark.asyncio
async def test_merged_incident_is_excluded(app_factory: AppMaker, tmp_path: Path) -> None:
    """A merged incident is excluded."""
    async with app_factory("test-token") as (app, token, _):
        client = in_process(app, token)
        project_id = await _create_project(client, tmp_path)

        starts = _day_starts(date(2026, 7, 1), 10)
        key = [
            Record(
                kind="null_spike",
                category="data_quality",
                column="col_a",
                start="2026-07-03",
                end="2026-07-05",
            )
        ]

        services = app.state.services
        inc_dt = datetime(2026, 7, 4, 10, 0, 0, tzinfo=UTC)
        draft1 = SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="col_a",
            model=None,
            ran_at=inc_dt,
            detail={},
            result_id=None,
            cleared=False,
        )
        inc1 = await services.incidents.raise_signal(project_id, draft1, seen_at=inc_dt)

        draft2 = SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="col_b",
            model=None,
            ran_at=inc_dt + timedelta(minutes=5),
            detail={},
            result_id=None,
            cleared=False,
        )
        inc2 = await services.incidents.raise_signal(
            project_id, draft2, seen_at=inc_dt + timedelta(minutes=5)
        )

        # Merge inc2 into inc1
        await services.incidents.merge(
            project_id, [inc1.incident_id, inc2.incident_id], agent="test"
        )

        res = await score(client, project_id, key, starts)
        assert isinstance(res, Score)

        # The merged incident must be in excluded and left unjudged
        assert inc2.incident_id in res.excluded
        assert inc2.incident_id not in res.verdicts
        assert inc2.incident_id not in res.matched

        # inc1 is the survivor, so it is judged
        assert inc1.incident_id in res.matched
        assert res.verdicts[inc1.incident_id] == VerdictDraft(value="real_issue")


@pytest.mark.asyncio
async def test_already_scored_user_verdict_refuses(app_factory: AppMaker, tmp_path: Path) -> None:
    """A project with a user verdict -> AlreadyScored, nothing written."""
    async with app_factory("test-token") as (app, token, _):
        client = in_process(app, token)
        project_id = await _create_project(client, tmp_path)

        starts = _day_starts(date(2026, 7, 1), 10)
        key = [
            Record(
                kind="null_spike",
                category="data_quality",
                column="col_a",
                start="2026-07-03",
                end="2026-07-05",
            )
        ]

        services = app.state.services
        inc_dt = datetime(2026, 7, 4, 10, 0, 0, tzinfo=UTC)
        draft = SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="col_a",
            model=None,
            ran_at=inc_dt,
            detail={},
            result_id=None,
            cleared=False,
        )
        outcome = await services.incidents.raise_signal(project_id, draft, seen_at=inc_dt)

        # Record a user verdict beforehand
        await client.post(
            f"/projects/{project_id}/incidents/{outcome.incident_id}/verdict",
            body={"value": "real_issue"},
        )

        with pytest.raises(AlreadyScored):
            await score(client, project_id, key, starts)

        # Ensure no misses were written
        misses = await client.get(f"/projects/{project_id}/misses")
        assert misses == []


@pytest.mark.asyncio
async def test_already_scored_miss_refuses(app_factory: AppMaker, tmp_path: Path) -> None:
    """A project with an existing miss -> AlreadyScored, nothing written."""
    async with app_factory("test-token") as (app, token, _):
        client = in_process(app, token)
        project_id = await _create_project(client, tmp_path)

        starts = _day_starts(date(2026, 7, 1), 10)
        key = [
            Record(
                kind="null_spike",
                category="data_quality",
                column="col_a",
                start="2026-07-03",
                end="2026-07-05",
            )
        ]

        # Post an existing miss beforehand
        await client.post(
            f"/projects/{project_id}/misses",
            body={
                "description": "Pre-existing miss",
                "category": "data_quality",
                "occurred_at": datetime(2026, 7, 3, 8, 0, 0, tzinfo=UTC).isoformat(),
            },
        )

        with pytest.raises(AlreadyScored):
            await score(client, project_id, key, starts)


@pytest.mark.asyncio
async def test_labels_verdict_is_replaced(app_factory: AppMaker, tmp_path: Path) -> None:
    """A labels verdict is replaced (not AlreadyScored, overwritten)."""
    async with app_factory("test-token") as (app, token, _):
        client = in_process(app, token)
        project_id = await _create_project(client, tmp_path)

        starts = _day_starts(date(2026, 7, 1), 10)
        key = [
            Record(
                kind="null_spike",
                category="data_quality",
                column="col_a",
                start="2026-07-03",
                end="2026-07-05",
            )
        ]

        services = app.state.services
        inc_dt = datetime(2026, 7, 4, 10, 0, 0, tzinfo=UTC)
        draft = SignalDraft(
            kind="fired_result",
            category="data_quality",
            subject="col_a",
            model=None,
            ran_at=inc_dt,
            detail={},
            result_id=None,
            cleared=False,
        )
        outcome = await services.incidents.raise_signal(project_id, draft, seen_at=inc_dt)

        # Record a verdict beforehand and set source == 'labels'
        await services.verdicts.record(
            project_id,
            outcome.incident_id,
            VerdictDraft(value="not_an_issue"),
            by="labels",
        )
        from mlops_agents.storage.tables import verdicts as verdicts_table
        from sqlalchemy import update

        engine = await services.databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                update(verdicts_table)
                .where(verdicts_table.c.incident_id == outcome.incident_id)
                .values(source="labels")
            )

        existing = await client.get(f"/projects/{project_id}/verdicts")
        assert len(existing) == 1
        assert existing[0]["source"] == "labels"

        # score should NOT raise AlreadyScored, and should overwrite the verdict with real_issue
        res = await score(client, project_id, key, starts)
        assert isinstance(res, Score)

        assert outcome.incident_id in res.matched
        assert res.verdicts[outcome.incident_id] == VerdictDraft(value="real_issue")

        updated = await client.get(f"/projects/{project_id}/verdicts")
        assert len(updated) == 1
        assert updated[0]["value"] == "real_issue"
        assert updated[0]["source"] == "user"


@pytest.mark.asyncio
async def test_silent_team_alert_scoring(app_factory: AppMaker, tmp_path: Path) -> None:
    """A silent team alert inside a window -> real_issue, outside -> not_an_issue."""
    async with app_factory("test-token") as (app, token, _):
        client = in_process(app, token)
        project_id = await _create_project(client, tmp_path)

        starts = _day_starts(date(2026, 7, 1), 25)
        # Problem: start 2026-07-03, end 2026-07-05. Grace: to 2026-07-12.
        key = [
            Record(
                kind="null_spike",
                category="data_quality",
                column="col_a",
                start="2026-07-03",
                end="2026-07-05",
            )
        ]

        services = app.state.services
        s_id = source_id("file", "*.jsonl", "conn1")

        # Team fire 1: ran_at on 2026-07-04 (inside window)
        dt_inside = datetime(2026, 7, 4, 10, 0, 0, tzinfo=UTC)
        rec_inside = ResultRecord(
            check_id="check_team_1",
            origin="team",
            ran_at=dt_inside,
            fired=True,
            category="data_quality",
            subject="col_a",
            model=None,
            detail={},
            source_ref="results.jsonl:check_team_1",
        )
        stored_inside = await services.results.ingest(
            project_id, s_id, [("k1", rec_inside)], seen_at=dt_inside
        )

        # Team fire 2: ran_at on 2026-07-20 (outside window and grace)
        dt_outside = datetime(2026, 7, 20, 10, 0, 0, tzinfo=UTC)
        rec_outside = ResultRecord(
            check_id="check_team_2",
            origin="team",
            ran_at=dt_outside,
            fired=True,
            category="data_quality",
            subject="col_a",
            model=None,
            detail={},
            source_ref="results.jsonl:check_team_2",
        )
        stored_outside = await services.results.ingest(
            project_id, s_id, [("k2", rec_outside)], seen_at=dt_outside
        )

        silent_alerts = await client.get(f"/projects/{project_id}/baseline/silent-alerts")
        assert len(silent_alerts) == 2

        res = await score(client, project_id, key, starts)
        assert isinstance(res, Score)

        assert res.silent[stored_inside[0].id] == "real_issue"
        assert res.silent[stored_outside[0].id] == "not_an_issue"

        # Check judged silent alerts in baseline
        judged = await services.baseline.judged_silent_alerts(project_id)
        judged_map = {alert.result_id: alert.verdict for alert in judged}
        assert judged_map[stored_inside[0].id] == "real_issue"
        assert judged_map[stored_outside[0].id] == "not_an_issue"


@pytest.mark.asyncio
async def test_incidents_paging(app_factory: AppMaker, tmp_path: Path) -> None:
    """Verifies that scoring pages through incidents (more than one page)."""
    async with app_factory("test-token") as (app, token, _):
        client = in_process(app, token)
        project_id = await _create_project(client, tmp_path)

        starts = _day_starts(date(2026, 7, 1), 10)
        # Problem spans the entire window so all incidents match
        key = [
            Record(
                kind="null_spike",
                category="data_quality",
                column="col_a",
                start="2026-07-01",
                end="2026-07-09",
            )
        ]

        services = app.state.services
        # Raise 60 incidents (default limit is 50, our paging will use limit=50 or 100)
        inc_ids: list[str] = []
        for i in range(55):
            dt = datetime(2026, 7, 2, 8, 0, 0, tzinfo=UTC) + timedelta(minutes=i)
            draft = SignalDraft(
                kind="fired_result",
                category="data_quality",
                subject=f"subj_{i}",
                model=None,
                ran_at=dt,
                detail={},
                result_id=None,
                cleared=False,
            )
            outcome = await services.incidents.raise_signal(project_id, draft, seen_at=dt)
            inc_ids.append(outcome.incident_id)

        # Exercise multi-page pagination by setting page_limit=20 for 55 incidents (3 pages: 20, 20, 15)
        res = await score(client, project_id, key, starts, page_limit=20)
        assert isinstance(res, Score)

        assert len(res.verdicts) == 55
        for inc_id in inc_ids:
            assert inc_id in res.matched
            assert res.verdicts[inc_id] == VerdictDraft(value="real_issue")
