"""Unit tests for RuleAuthority (I23, I30)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mlops_agents.agents.approvals.authority import ActionCall
from mlops_agents.clock import to_iso
from mlops_agents.ids import new_id
from mlops_agents.monitoring.expr import Compare
from mlops_agents.monitoring.results.contract import ResultRecord, source_id
from mlops_agents.monitoring.results.store import ResultsStore
from mlops_agents.monitoring.rules.authority import RuleAuthority
from mlops_agents.monitoring.rules.contract import (
    RuleLimits,
    RuleSpec,
    RuleTerm,
)
from mlops_agents.monitoring.rules.store import SqliteRules
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import hook_runs
from tests.monitoring.rules.conftest import ResultsAdapter
from tests.providers.fake_clock import FakeClock


def _make_spec(
    hook_id: str = "hok_rb",
    hook_kind: str = "rollback",
    model: str | None = "churn",
    check_id: str = "chk_drift",
    per_incident: int = 1,
    per_day: int = 2,
) -> RuleSpec:
    return RuleSpec(
        name="test-authority-rule",
        why="authorize rollback on drift",
        model=model,
        terms=[
            RuleTerm(
                check_id=check_id,
                model=model,
                when=Compare(op="gt", field="value", value=0.5),
                window_minutes=60,
                quantifier="latest",
                min_records=1,
            )
        ],
        hook_id=hook_id,
        hook_kind=hook_kind,  # type: ignore[arg-type]
        limits=RuleLimits(per_incident=per_incident, per_day=per_day),
    )


def _make_record(
    check_id: str = "chk_drift",
    model: str | None = "churn",
    value: float = 0.8,
    ran_at: datetime | None = None,
) -> ResultRecord:
    return ResultRecord(
        check_id=check_id,
        origin="agent",
        ran_at=ran_at or datetime(2026, 10, 31, 6, 30, 0, tzinfo=UTC),
        fired=True,
        value=value,
        threshold=0.5,
        category="drift",
        subject="predictions",
        model=model,
        detail={},
        source_ref="checks/drift.py",
    )


@pytest.mark.asyncio
async def test_authority_returns_basis_rule_with_evidence_when_valid(
    databases: Databases,
) -> None:
    clock = FakeClock(start=datetime(2026, 10, 31, 7, 0, 0, tzinfo=UTC))
    rules = SqliteRules(databases, clock=clock)
    results = ResultsAdapter(ResultsStore(databases))
    project_id = new_id("prj")

    rule = await rules.store(project_id, _make_spec(), proposal_id="prp_1")

    # Ingest a satisfying result
    rec = _make_record(ran_at=clock.now() - timedelta(minutes=10), value=0.8)
    s_id = source_id("file", "*.jsonl", "data_conn")
    stored = await results.ingest(project_id, s_id, [("k1", rec)], seen_at=clock.now())
    rec_id = stored[0].id

    authority = RuleAuthority(rules=rules, results=results, databases=databases, clock=clock)

    call = ActionCall(
        project_id=project_id,
        agent="test_agent",
        kind="rollback",
        hook_id="hok_rb",
        model="churn",
        incident_id="inc_001",
        rule_id=rule.id,
    )

    auth = await authority.authorize(call)
    assert auth.basis == "rule"
    assert auth.ref == rule.id
    assert auth.evidence == [rec_id]
    assert "satisfied" in auth.reason.lower()


@pytest.mark.asyncio
async def test_authority_returns_basis_none_when_checks_fail(
    databases: Databases,
) -> None:
    clock = FakeClock(start=datetime(2026, 10, 31, 7, 0, 0, tzinfo=UTC))
    rules = SqliteRules(databases, clock=clock)
    results = ResultsAdapter(ResultsStore(databases))
    project_id = new_id("prj")

    rule = await rules.store(project_id, _make_spec(), proposal_id="prp_1")

    # Ingest satisfying result
    rec = _make_record(ran_at=clock.now() - timedelta(minutes=10), value=0.8)
    s_id = source_id("file", "*.jsonl", "data_conn")
    await results.ingest(project_id, s_id, [("k1", rec)], seen_at=clock.now())

    authority = RuleAuthority(rules=rules, results=results, databases=databases, clock=clock)

    # 1. Missing or invalid rule_id
    call_no_rule = ActionCall(
        project_id=project_id,
        agent="test_agent",
        kind="rollback",
        hook_id="hok_rb",
        model="churn",
        incident_id="inc_001",
        rule_id=None,
    )
    auth = await authority.authorize(call_no_rule)
    assert auth.basis == "none"

    call_unknown_rule = ActionCall(
        project_id=project_id,
        agent="test_agent",
        kind="rollback",
        hook_id="hok_rb",
        model="churn",
        incident_id="inc_001",
        rule_id="rul_nonexistent",
    )
    auth = await authority.authorize(call_unknown_rule)
    assert auth.basis == "none"

    # 2. Disabled rule
    await rules.set_enabled(project_id, rule.id, False)
    call = ActionCall(
        project_id=project_id,
        agent="test_agent",
        kind="rollback",
        hook_id="hok_rb",
        model="churn",
        incident_id="inc_001",
        rule_id=rule.id,
    )
    auth = await authority.authorize(call)
    assert auth.basis == "none"
    assert "disabled" in auth.reason.lower()

    # Re-enable rule for further checks
    await rules.set_enabled(project_id, rule.id, True)

    # 3. Another hook_id
    call_diff_hook = call.model_copy(update={"hook_id": "hok_other"})
    auth = await authority.authorize(call_diff_hook)
    assert auth.basis == "none"

    # 4. Another kind
    call_diff_kind = call.model_copy(update={"kind": "rerun"})
    auth = await authority.authorize(call_diff_kind)
    assert auth.basis == "none"

    # 5. Another model
    call_diff_model = call.model_copy(update={"model": "other_model"})
    auth = await authority.authorize(call_diff_model)
    assert auth.basis == "none"

    # 6. No incident_id
    call_no_inc = call.model_copy(update={"incident_id": None})
    auth = await authority.authorize(call_no_inc)
    assert auth.basis == "none"

    # 7. Failing term: update result or advance clock past window
    clock.advance(7200.0)  # 2 hours later, result is outside 60-min window
    auth = await authority.authorize(call)
    assert auth.basis == "none"


@pytest.mark.asyncio
async def test_authority_returns_basis_none_when_limits_reached(
    databases: Databases,
) -> None:
    clock = FakeClock(start=datetime(2026, 10, 31, 7, 0, 0, tzinfo=UTC))
    rules = SqliteRules(databases, clock=clock)
    results = ResultsAdapter(ResultsStore(databases))
    project_id = new_id("prj")

    rule = await rules.store(
        project_id, _make_spec(per_incident=1, per_day=2), proposal_id="prp_lim"
    )

    rec = _make_record(ran_at=clock.now() - timedelta(minutes=10), value=0.8)
    s_id = source_id("file", "*.jsonl", "data_conn")
    await results.ingest(project_id, s_id, [("k1", rec)], seen_at=clock.now())

    authority = RuleAuthority(rules=rules, results=results, databases=databases, clock=clock)

    call = ActionCall(
        project_id=project_id,
        agent="test_agent",
        kind="rollback",
        hook_id="hok_rb",
        model="churn",
        incident_id="inc_lim",
        rule_id=rule.id,
    )

    # First call holds
    auth = await authority.authorize(call)
    assert auth.basis == "rule"

    # Insert a hook_run row simulating that this rule ran on this incident
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            hook_runs.insert().values(
                id=new_id("hok"),
                hook_id="hok_rb",
                kind="rollback",
                model="churn",
                incident_id="inc_lim",
                rule_id=rule.id,
                reason="rollback",
                basis="rule",
                evidence="[]",
                started=to_iso(clock.now()),
            )
        )

    # 8. per_incident limit reached
    auth = await authority.authorize(call)
    assert auth.basis == "none"
    assert "per_incident limit" in auth.reason

    # Call on another incident
    call_other_inc = call.model_copy(update={"incident_id": "inc_lim_2"})
    auth = await authority.authorize(call_other_inc)
    assert auth.basis == "rule"

    # Insert second hook_run on inc_lim_2
    async with engine.begin() as conn:
        await conn.execute(
            hook_runs.insert().values(
                id=new_id("hok"),
                hook_id="hok_rb",
                kind="rollback",
                model="churn",
                incident_id="inc_lim_2",
                rule_id=rule.id,
                reason="rollback",
                basis="rule",
                evidence="[]",
                started=to_iso(clock.now()),
            )
        )

    # 9. per_day limit (2) reached
    call_third_inc = call.model_copy(update={"incident_id": "inc_lim_3"})
    auth = await authority.authorize(call_third_inc)
    assert auth.basis == "none"
    assert "per_day limit" in auth.reason
