"""A fixture project with known rows of every kind gives the expected value and n per metric."""

from __future__ import annotations

from datetime import timedelta

import pytest

from mlops_agents.clock import to_iso
from mlops_agents.monitoring.incidents.contract import IncidentPatch
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.monitoring.verdicts.baseline import SqliteBaseline
from mlops_agents.monitoring.verdicts.contract import MissReport, VerdictDraft
from mlops_agents.monitoring.verdicts.store import SqliteVerdicts
from mlops_agents.providers.usage_contract import ModelPrice
from mlops_agents.stats.contract import STAT_METRICS, StatsView
from mlops_agents.stats.service import StatsService
from mlops_agents.storage.databases import Databases
from tests.providers.fake_clock import FakeClock
from tests.providers.usage_seed import seed_calls
from tests.stats.conftest import (
    NOW,
    FakeModels,
    add_approvals,
    add_events,
    add_hook_runs,
    add_messages,
    add_tasks,
    add_turns,
    ago,
    raise_on,
    result_record,
    store,
    team_fire,
)

PRICED = "fake:priced"
OTHER = "fake:other"

# (value, n) per metric for the 30-day window, no category. The rows behind each are seeded
# in `_seed`; the comment says how the value comes out of them.
EXPECTED: dict[str, tuple[float | None, int]] = {
    "precision": (2 / 4, 4),  # real_issue on inc 1 and 2 of 4 verdicts
    "caught_baseline_missed": (1, 4),  # inc 2; the team caught inc 1
    "false_alarms_avoided": (1, 2),  # silent_a; silent_c is out of the window
    "misses": (2, 2),  # one miss report in the window + silent_b
    "detection_delay_s": (900, 2),  # median of 600 and 1200
    "time_to_resolution_s": (86_400 - 600, 1),  # inc 1
    "alerts_per_week": (5 / (30 / 7), 5),
    "signals_per_incident": (7 / 5, 5),  # inc 2 has an attach, inc 5 absorbed inc 7
    "bad_data_vs_drift_correct": (1 / 2, 2),  # inc 2 real_issue, inc 3 wrong_type
    "autonomous_share": (3 / 5, 5),  # rule, policy and a hold of five runs
    "approvals_requested": (5, 5),
    "acceptance_rate": (2 / 3, 3),  # two approved, one rejected
    "prs_opened": (2, 2),
    "prs_merged": (1, 1),
    "prs_closed": (1, 1),
    "metric_before": (0.65, 1),
    "metric_after": (0.85, 1),
    "tokens_per_agent": (3700 / 2, 2),
    "cost_per_incident": (8200 / 1_000_000 / 5, 5),
    "failed_runs": (2, 2),
    "tool_errors": (2, 2),
    "sandbox_failures": (1, 1),
}


async def _incidents(
    incidents: IncidentsService,
    verdicts: SqliteVerdicts,
    results: ResultsService,
    clock: FakeClock,
    project_id: str,
) -> None:
    started_1 = ago(days=10)
    inc_1 = await raise_on(
        incidents,
        project_id,
        subject="s1",
        started_at=started_1,
        detected_at=started_1 + timedelta(seconds=600),
    )
    clock.current = started_1 + timedelta(days=1)
    await incidents.update(project_id, inc_1, "orchestrator", IncidentPatch(status="resolved"))
    clock.current = NOW
    await verdicts.record(project_id, inc_1, VerdictDraft(value="real_issue"), by="user")
    # The team's check fired five minutes in: the team caught inc 1.
    await team_fire(results, project_id, started_1 + timedelta(minutes=5), subject="s1")

    started_2 = ago(days=5)
    inc_2 = await raise_on(
        incidents,
        project_id,
        subject="s2",
        model=None,
        category="data_quality",
        started_at=started_2,
        detected_at=started_2 + timedelta(seconds=1200),
    )
    await raise_on(
        incidents,
        project_id,
        subject="s2",
        model=None,
        category="data_quality",
        started_at=started_2 + timedelta(hours=1),
    )
    await verdicts.record(project_id, inc_2, VerdictDraft(value="real_issue"), by="user")

    inc_3 = await raise_on(
        incidents, project_id, subject="s3", model=None, category="drift", started_at=ago(days=3)
    )
    wrong = VerdictDraft(value="wrong_type", right_category="data_quality")
    await verdicts.record(project_id, inc_3, wrong, by="user")

    inc_4 = await raise_on(
        incidents,
        project_id,
        subject="s4",
        model=None,
        category="operational",
        started_at=ago(days=2),
    )
    await verdicts.record(project_id, inc_4, VerdictDraft(value="not_an_issue"), by="user")

    inc_5 = await raise_on(
        incidents,
        project_id,
        subject="s5",
        model=None,
        category="data_quality",
        started_at=ago(days=1),
    )
    inc_7 = await raise_on(
        incidents,
        project_id,
        subject="s7",
        model=None,
        category="data_quality",
        started_at=ago(days=1) + timedelta(hours=1),
    )
    await incidents.merge(project_id, [inc_5, inc_7], agent="orchestrator")

    # Out of the 30-day window: its verdict counts nowhere.
    inc_6 = await raise_on(
        incidents, project_id, subject="s6", model=None, category="drift", started_at=ago(days=40)
    )
    await verdicts.record(project_id, inc_6, VerdictDraft(value="real_issue"), by="user")


async def _label_results(results: ResultsService, project_id: str) -> None:
    """Label-based results for inc 1 (detected 10 days less 600 s ago, resolved 9 days ago)."""
    detected = ago(days=10) + timedelta(seconds=600)
    resolved = ago(days=9)
    rows = [
        result_record(detected - timedelta(hours=2), subject="s1", origin="agent", value=0.70),
        result_record(detected - timedelta(seconds=60), subject="s1", origin="agent", value=0.65),
        # A not-enough-data record never counts, nor does a team result.
        result_record(
            detected - timedelta(seconds=30),
            subject="s1",
            origin="agent",
            value=0.10,
            not_enough_data=True,
        ),
        result_record(resolved + timedelta(hours=1), subject="s1", origin="agent", value=0.80),
        result_record(resolved + timedelta(hours=2), subject="s1", origin="agent", value=0.85),
        result_record(resolved + timedelta(hours=3), subject="s1", fired=False, value=0.99),
    ]
    for row in rows:
        await store(results, project_id, row.model_copy(update={"fired": False}))


async def _silent_alerts(
    baseline: SqliteBaseline, verdicts: SqliteVerdicts, results: ResultsService, project_id: str
) -> None:
    silent_a = await team_fire(results, project_id, ago(days=2), subject="silent_a")
    silent_b = await team_fire(results, project_id, ago(days=1), subject="silent_b")
    silent_c = await team_fire(results, project_id, ago(days=40), subject="silent_c")
    await team_fire(results, project_id, ago(days=1), subject="silent_d")
    await baseline.judge_silent(project_id, silent_a.id, "not_an_issue", by="user")
    await baseline.judge_silent(project_id, silent_b.id, "real_issue", by="user")
    await baseline.judge_silent(project_id, silent_c.id, "not_an_issue", by="user")
    await verdicts.report_miss(
        project_id, MissReport(description="late", occurred_at=ago(days=4), reported_at=ago(days=3))
    )
    # Reported in the window, but it happened before it.
    await verdicts.report_miss(
        project_id, MissReport(description="old", occurred_at=ago(days=35), reported_at=ago(days=1))
    )


async def _stage_2_rows(databases: Databases, project_id: str) -> None:
    await add_hook_runs(
        databases,
        project_id,
        [
            ("command", "rule", ago(days=1)),
            ("command", "policy", ago(days=2)),
            ("command", "approval", ago(days=3)),
            ("command", "none", ago(days=4)),
            ("hold", "approval", ago(days=5)),
            ("command", "rule", ago(days=31)),
        ],
    )
    await add_approvals(
        databases,
        project_id,
        [
            ("approved", ago(days=1)),
            ("approved", ago(days=2)),
            ("rejected", ago(days=3)),
            ("pending", ago(days=4)),
            ("expired", ago(days=5)),
            ("approved", ago(days=31)),
        ],
    )
    await add_tasks(
        databases,
        project_id,
        [
            ("merged", 11, ago(days=5), ago(days=4)),
            ("closed", 12, ago(days=40), ago(days=3)),
            ("active", 13, ago(days=2), None),
            # Merged locally, without a PR: no PR number counts it.
            ("merged", None, ago(days=2), ago(days=1)),
        ],
    )
    await add_turns(
        databases,
        project_id,
        [
            ("failed", ago(days=1)),
            ("failed", ago(days=2)),
            ("done", ago(days=3)),
            ("failed", ago(days=31)),
        ],
    )
    error = {"type": "tool_result", "tool_call_id": "c1", "content": "boom", "is_error": True}
    fine = {"type": "tool_result", "tool_call_id": "c2", "content": "ok", "is_error": False}
    # Key order is not the parser's concern.
    reordered = {"is_error": True, "content": "boom", "tool_call_id": "c3", "type": "tool_result"}
    await add_messages(
        databases,
        project_id,
        [
            ([error, fine, {"type": "text", "text": "hi"}], ago(days=1)),
            ([reordered], ago(days=2)),
            ([error], ago(days=31)),
        ],
    )
    await add_events(
        databases,
        project_id,
        [
            ("sandbox.build.failed", ago(days=1)),
            ("sandbox.build.finished", ago(days=2)),
            ("sandbox.build.failed", ago(days=31)),
        ],
    )


async def _llm_calls(databases: Databases, models: FakeModels, project_id: str) -> None:
    models.prices[PRICED] = ModelPrice(prompt_per_mtok=2, completion_per_mtok=4, source="user")
    models.prices[OTHER] = ModelPrice(prompt_per_mtok=1, completion_per_mtok=1, source="user")
    in_window = to_iso(ago(days=1))
    old = to_iso(ago(days=31))
    await seed_calls(
        databases,
        project_id,
        [
            ("orchestrator", PRICED, 1000, 500, in_window),
            ("auditor", PRICED, 2000, None, in_window),
            ("auditor", OTHER, 100, 100, in_window),
            ("engineer", PRICED, 9999, 9999, old),
        ],
    )


@pytest.fixture
async def fixture_project(
    databases: Databases,
    incidents: IncidentsService,
    verdicts: SqliteVerdicts,
    baseline: SqliteBaseline,
    results: ResultsService,
    models: FakeModels,
    clock: FakeClock,
    project_id: str,
) -> str:
    await _incidents(incidents, verdicts, results, clock, project_id)
    await _label_results(results, project_id)
    await _silent_alerts(baseline, verdicts, results, project_id)
    await _stage_2_rows(databases, project_id)
    await _llm_calls(databases, models, project_id)
    return project_id


def _flat(view: StatsView) -> dict[str, tuple[float | None, int]]:
    return {
        name: (stat.value, stat.n) for group in view.groups.values() for name, stat in group.items()
    }


async def test_every_metric_has_its_expected_value_and_n(
    stats: StatsService, fixture_project: str
) -> None:
    view = await stats.compute(fixture_project, "30d", None, NOW)

    assert view.baseline_available
    assert set(EXPECTED) == {name for names in STAT_METRICS.values() for name in names}
    assert {g: list(m) for g, m in view.groups.items()} == {
        g: list(names) for g, names in STAT_METRICS.items()
    }
    got = _flat(view)
    for names in STAT_METRICS.values():
        for name in names:
            value, n = EXPECTED[name]
            assert got[name][1] == n, name
            assert got[name][0] == pytest.approx(value), name


async def test_a_category_narrows_the_incident_groups_only(
    stats: StatsService, fixture_project: str
) -> None:
    got = _flat(await stats.compute(fixture_project, "30d", "data_quality", NOW))

    # inc 2 and inc 5 are the data-quality incidents in the window; only inc 2 has a verdict.
    assert got["precision"] == (1.0, 1)
    assert got["caught_baseline_missed"] == (1, 1)
    assert got["detection_delay_s"] == (1200, 1)
    assert got["time_to_resolution_s"] == (None, 0)
    assert got["alerts_per_week"][1] == 2
    assert got["signals_per_incident"] == (2.0, 2)
    assert got["bad_data_vs_drift_correct"] == (1.0, 1)
    assert got["metric_before"] == (None, 0)
    assert got["metric_after"] == (None, 0)
    unfiltered = _flat(await stats.compute(fixture_project, "30d", None, NOW))
    for name in (
        "false_alarms_avoided",
        "misses",
        "autonomous_share",
        "approvals_requested",
        "acceptance_rate",
        "prs_opened",
        "prs_merged",
        "prs_closed",
        "tokens_per_agent",
        "failed_runs",
        "tool_errors",
        "sandbox_failures",
    ):
        assert got[name] == unfiltered[name], name


async def test_a_category_filter_leaves_cost_per_incident_without_a_value(
    stats: StatsService, fixture_project: str
) -> None:
    got = _flat(await stats.compute(fixture_project, "30d", "data_quality", NOW))

    # Tokens carry no category, so no cost can be told for one category's incidents.
    assert got["cost_per_incident"] == (None, 2)
    unfiltered = _flat(await stats.compute(fixture_project, "30d", None, NOW))
    assert unfiltered["cost_per_incident"][0] == pytest.approx(8200 / 1_000_000 / 5)


async def test_a_used_model_without_a_price_makes_cost_per_incident_none(
    stats: StatsService, fixture_project: str, databases: Databases, models: FakeModels
) -> None:
    models.prices[OTHER] = None
    got = _flat(await stats.compute(fixture_project, "30d", None, NOW))
    assert got["cost_per_incident"] == (None, 5)

    # A model deleted from the list has no price either.
    del models.prices[OTHER]
    got = _flat(await stats.compute(fixture_project, "30d", None, NOW))
    assert got["cost_per_incident"] == (None, 5)
    assert got["tokens_per_agent"] == (3700 / 2, 2)
