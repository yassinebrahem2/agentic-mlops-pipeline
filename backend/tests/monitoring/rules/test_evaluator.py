"""Unit tests for rule evaluator and rule_limits_allow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mlops_agents.clock import to_iso
from mlops_agents.ids import new_id
from mlops_agents.monitoring.expr import Compare
from mlops_agents.monitoring.results.contract import ResultRecord, source_id
from mlops_agents.monitoring.results.store import ResultsStore
from mlops_agents.monitoring.rules.contract import (
    RuleLimits,
    RuleSpec,
    RuleTerm,
    RuleView,
)
from mlops_agents.monitoring.rules.evaluator import (
    evaluate_rule,
    rule_limits_allow,
)
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import hook_runs
from tests.monitoring.rules.conftest import ResultsAdapter


def _make_rule(
    terms: list[RuleTerm],
    hook_id: str = "hok_test",
    hook_kind: str = "rollback",
    model: str | None = "churn",
    per_incident: int = 1,
    per_day: int = 2,
    enabled: bool = True,
    rule_id: str = "rul_test",
) -> RuleView:
    return RuleView(
        id=rule_id,
        spec=RuleSpec(
            name="test-rule",
            why="test rule evaluation",
            model=model,
            terms=terms,
            hook_id=hook_id,
            hook_kind=hook_kind,  # type: ignore[arg-type]
            limits=RuleLimits(per_incident=per_incident, per_day=per_day),
        ),
        enabled=enabled,
        proposal_id="prp_001",
        approved=datetime(2026, 10, 31, 6, 0, 0, tzinfo=UTC),
    )


def _make_record(
    check_id: str = "chk_drift",
    ran_at: datetime | None = None,
    fired: bool = False,
    model: str | None = "churn",
    value: float | None = 0.8,
    detail: dict[str, Any] | None = None,
) -> ResultRecord:
    return ResultRecord(
        check_id=check_id,
        origin="agent",
        ran_at=ran_at or datetime(2026, 10, 31, 6, 30, 0, tzinfo=UTC),
        fired=fired,
        value=value,
        threshold=0.5,
        category="drift",
        subject="predictions",
        model=model,
        detail=detail if detail is not None else {"drift_score": 0.8},
        source_ref="checks/drift.py",
    )


@pytest.mark.asyncio
async def test_evaluator_quantifiers(databases: Databases) -> None:
    store = ResultsStore(databases)
    results = ResultsAdapter(store)
    project_id = new_id("prj")
    s_id = source_id("file", "*.jsonl", "data_conn")
    t0 = datetime(2026, 10, 31, 6, 0, 0, tzinfo=UTC)

    # Ingest records: older at t0+10m (value 0.3), newer at t0+20m (value 0.8)
    rec1 = _make_record(ran_at=t0 + timedelta(minutes=10), value=0.3)
    rec2 = _make_record(ran_at=t0 + timedelta(minutes=20), value=0.8)
    inserted = await store.insert_items(project_id, s_id, [("k1", rec1), ("k2", rec2)], seen_at=t0)
    # inserted are StoredResults
    rec1_id = next(r.id for r in inserted if r.value == 0.3)
    rec2_id = next(r.id for r in inserted if r.value == 0.8)

    now = t0 + timedelta(minutes=30)

    # 1. Quantifier 'latest': condition holds for newest (rec2 has value 0.8 > 0.5)
    term_latest = RuleTerm(
        check_id="chk_drift",
        model="churn",
        when=Compare(op="gt", field="value", value=0.5),
        window_minutes=60,
        quantifier="latest",
        min_records=1,
    )
    rule_latest = _make_rule([term_latest])
    passed, evidence = await evaluate_rule(results, project_id, rule_latest, now)
    assert passed is True
    assert evidence == [rec2_id]

    # If condition does not hold for newest: value < 0.5 (rec2 has 0.8, not < 0.5)
    term_latest_fail = RuleTerm(
        check_id="chk_drift",
        model="churn",
        when=Compare(op="lt", field="value", value=0.5),
        window_minutes=60,
        quantifier="latest",
        min_records=1,
    )
    rule_latest_fail = _make_rule([term_latest_fail])
    passed, evidence = await evaluate_rule(results, project_id, rule_latest_fail, now)
    assert passed is False
    assert evidence == []

    # 2. Quantifier 'any': rec1 (0.3) does not hold, rec2 (0.8) holds -> passes with evidence rec2
    term_any = RuleTerm(
        check_id="chk_drift",
        model="churn",
        when=Compare(op="gt", field="value", value=0.5),
        window_minutes=60,
        quantifier="any",
        min_records=1,
    )
    rule_any = _make_rule([term_any])
    passed, evidence = await evaluate_rule(results, project_id, rule_any, now)
    assert passed is True
    assert evidence == [rec2_id]

    # 3. Quantifier 'all': value > 0.2 holds for both -> passes with evidence [rec2_id, rec1_id]
    term_all = RuleTerm(
        check_id="chk_drift",
        model="churn",
        when=Compare(op="gt", field="value", value=0.2),
        window_minutes=60,
        quantifier="all",
        min_records=2,
    )
    rule_all = _make_rule([term_all])
    passed, evidence = await evaluate_rule(results, project_id, rule_all, now)
    assert passed is True
    assert set(evidence) == {rec1_id, rec2_id}

    # 'all' fails if at least one fails: value > 0.5 fails on rec1
    term_all_fail = RuleTerm(
        check_id="chk_drift",
        model="churn",
        when=Compare(op="gt", field="value", value=0.5),
        window_minutes=60,
        quantifier="all",
        min_records=2,
    )
    rule_all_fail = _make_rule([term_all_fail])
    passed, evidence = await evaluate_rule(results, project_id, rule_all_fail, now)
    assert passed is False
    assert evidence == []


@pytest.mark.asyncio
async def test_evaluator_min_records(databases: Databases) -> None:
    store = ResultsStore(databases)
    results = ResultsAdapter(store)
    project_id = new_id("prj")
    s_id = source_id("file", "*.jsonl", "data_conn")
    t0 = datetime(2026, 10, 31, 6, 0, 0, tzinfo=UTC)

    rec = _make_record(ran_at=t0 + timedelta(minutes=10), value=0.8)
    await store.insert_items(project_id, s_id, [("k1", rec)], seen_at=t0)

    now = t0 + timedelta(minutes=30)
    term = RuleTerm(
        check_id="chk_drift",
        model="churn",
        when=Compare(op="gt", field="value", value=0.5),
        window_minutes=60,
        quantifier="any",
        min_records=2,  # requires 2, but only 1 exists
    )
    rule = _make_rule([term])
    passed, evidence = await evaluate_rule(results, project_id, rule, now)
    assert passed is False
    assert evidence == []


@pytest.mark.asyncio
async def test_evaluator_window_edges(databases: Databases) -> None:
    store = ResultsStore(databases)
    results = ResultsAdapter(store)
    project_id = new_id("prj")
    s_id = source_id("file", "*.jsonl", "data_conn")
    now = datetime(2026, 10, 31, 7, 0, 0, tzinfo=UTC)
    window_minutes = 60
    # exactly 60 minutes ago: 6:00:00
    exact_edge = now - timedelta(minutes=window_minutes)
    one_sec_before = exact_edge - timedelta(seconds=1)

    rec_edge = _make_record(ran_at=exact_edge, value=0.8)
    rec_old = _make_record(ran_at=one_sec_before, value=0.8)
    inserted = await store.insert_items(
        project_id, s_id, [("edge", rec_edge), ("old", rec_old)], seen_at=now
    )
    edge_id = next(r.id for r in inserted if r.ran_at == exact_edge)

    term = RuleTerm(
        check_id="chk_drift",
        model="churn",
        when=Compare(op="gt", field="value", value=0.5),
        window_minutes=window_minutes,
        quantifier="any",
        min_records=1,
    )
    rule = _make_rule([term])
    passed, evidence = await evaluate_rule(results, project_id, rule, now)
    assert passed is True
    # rec_old is outside window (ran_at < now - window_minutes), so evidence only contains edge_id
    assert evidence == [edge_id]


@pytest.mark.asyncio
async def test_evaluator_missing_field_is_false(databases: Databases) -> None:
    store = ResultsStore(databases)
    results = ResultsAdapter(store)
    project_id = new_id("prj")
    s_id = source_id("file", "*.jsonl", "data_conn")
    now = datetime(2026, 10, 31, 7, 0, 0, tzinfo=UTC)

    rec = _make_record(ran_at=now - timedelta(minutes=5), detail={})
    await store.insert_items(project_id, s_id, [("k1", rec)], seen_at=now)

    term = RuleTerm(
        check_id="chk_drift",
        model="churn",
        when=Compare(op="gt", field="detail.non_existent_key", value=0.5),
        window_minutes=60,
        quantifier="latest",
        min_records=1,
    )
    rule = _make_rule([term])
    passed, evidence = await evaluate_rule(results, project_id, rule, now)
    assert passed is False
    assert evidence == []


@pytest.mark.asyncio
async def test_evaluator_another_model_records_not_counted(databases: Databases) -> None:
    store = ResultsStore(databases)
    results = ResultsAdapter(store)
    project_id = new_id("prj")
    s_id = source_id("file", "*.jsonl", "data_conn")
    now = datetime(2026, 10, 31, 7, 0, 0, tzinfo=UTC)

    # 1. Term specifies model="churn", records exist for "other" and None
    rec_other = _make_record(ran_at=now - timedelta(minutes=5), model="other", value=0.9)
    rec_none = _make_record(ran_at=now - timedelta(minutes=5), model=None, value=0.9)
    await store.insert_items(
        project_id, s_id, [("other", rec_other), ("none", rec_none)], seen_at=now
    )

    term_churn = RuleTerm(
        check_id="chk_drift",
        model="churn",
        when=Compare(op="gt", field="value", value=0.5),
        window_minutes=60,
        quantifier="any",
        min_records=1,
    )
    rule_churn = _make_rule([term_churn], model="churn")
    passed, evidence = await evaluate_rule(results, project_id, rule_churn, now)
    assert passed is False
    assert evidence == []

    # 2. Term specifies model=None, record exists for "churn" and None
    term_none = RuleTerm(
        check_id="chk_drift",
        model=None,
        when=Compare(op="gt", field="value", value=0.5),
        window_minutes=60,
        quantifier="any",
        min_records=1,
    )
    rule_none = _make_rule([term_none], model=None)
    passed, evidence = await evaluate_rule(results, project_id, rule_none, now)
    # rec_none is counted and satisfies value > 0.5
    assert passed is True
    assert len(evidence) == 1


@pytest.mark.asyncio
async def test_rule_limits_allow(databases: Databases) -> None:
    project_id = new_id("prj")
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    rule = _make_rule([], rule_id="rul_lim", per_incident=1, per_day=2)
    incident_id = "inc_test_1"

    # Initially count is 0 for both: allowed
    allowed, reason = await rule_limits_allow(databases, project_id, rule, incident_id, now)
    assert allowed is True
    assert reason is None

    engine = await databases.project(project_id)

    # Insert 1 hook_run for rul_lim, basis="rule", on incident_id
    async with engine.begin() as conn:
        await conn.execute(
            hook_runs.insert().values(
                id=new_id("hok"),
                hook_id="hok_1",
                kind="rollback",
                model="churn",
                incident_id=incident_id,
                rule_id="rul_lim",
                reason="rule triggered",
                basis="rule",
                evidence="[]",
                started=to_iso(now - timedelta(hours=1)),
            )
        )

    # Now per_incident (limit 1) is reached for incident_id
    allowed, reason = await rule_limits_allow(databases, project_id, rule, incident_id, now)
    assert allowed is False
    assert reason == "per_incident limit of 1 reached"

    # But for another incident, per_incident is not reached: count on inc_test_2 is 0,
    # though per_day is currently 1 (out of 2).
    allowed, reason = await rule_limits_allow(databases, project_id, rule, "inc_test_2", now)
    assert allowed is True
    assert reason is None

    # Insert another hook_run for rul_lim, basis="rule", on inc_test_2 within 24h
    async with engine.begin() as conn:
        await conn.execute(
            hook_runs.insert().values(
                id=new_id("hok"),
                hook_id="hok_1",
                kind="rollback",
                model="churn",
                incident_id="inc_test_2",
                rule_id="rul_lim",
                reason="rule triggered 2",
                basis="rule",
                evidence="[]",
                started=to_iso(now - timedelta(hours=2)),
            )
        )

    # Now per_day (limit 2) is reached in the 24 hours (tested on a fresh incident)
    allowed, reason = await rule_limits_allow(databases, project_id, rule, "inc_test_3", now)
    assert allowed is False
    assert reason == "per_day limit of 2 reached"

    # Check that runs older than 24h do not count against per_day
    clock_future = now + timedelta(hours=23)  # now run1 is 24h old, run2 is 25h old
    allowed, reason = await rule_limits_allow(
        databases, project_id, rule, "inc_test_4", clock_future
    )
    assert allowed is True
    assert reason is None


@pytest.mark.asyncio
async def test_evaluator_empty_terms_returns_false(databases: Databases) -> None:
    store = ResultsStore(databases)
    results = ResultsAdapter(store)
    project_id = new_id("prj")
    now = datetime(2026, 10, 31, 7, 0, 0, tzinfo=UTC)

    rule = _make_rule([])
    passed, evidence = await evaluate_rule(results, project_id, rule, now)
    assert passed is False
    assert evidence == []


@pytest.mark.asyncio
async def test_evaluator_pages_through_results(databases: Databases) -> None:
    store = ResultsStore(databases)
    results = ResultsAdapter(store)
    project_id = new_id("prj")
    s_id = source_id("file", "*.jsonl", "data_conn")
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)

    # Ingest 550 records within a 600-minute window
    # When sorted by ran_at desc, index 0 is newest, index 549 is oldest.
    # Record at index 520 (an older record) fails the condition.
    # Without paging (limit=500), only indices 0..499 are fetched, all passing, so it would erroneously pass.
    # With paging, all 550 records are fetched, record 520 fails 'all', so it correctly returns False.
    total_records = 550
    failing_index = 520
    items: list[tuple[str, ResultRecord]] = []
    for idx in range(total_records):
        ran_at = now - timedelta(minutes=idx + 1)
        val = 0.1 if idx == failing_index else 0.9
        rec = _make_record(ran_at=ran_at, value=val)
        items.append((f"k_{idx}", rec))

    await store.insert_items(project_id, s_id, items, seen_at=now)

    term = RuleTerm(
        check_id="chk_drift",
        model="churn",
        when=Compare(op="gt", field="value", value=0.5),
        window_minutes=600,
        quantifier="all",
        min_records=1,
    )
    rule = _make_rule([term])
    passed, evidence = await evaluate_rule(results, project_id, rule, now)
    assert passed is False
    assert evidence == []
