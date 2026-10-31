"""Unit tests for SqliteRules store."""

from __future__ import annotations

import pytest

from mlops_agents.monitoring.expr import Compare
from mlops_agents.monitoring.rules.contract import (
    RuleLimits,
    Rules,
    RuleSpec,
    RuleTerm,
)
from mlops_agents.monitoring.rules.store import SqliteRules
from mlops_agents.storage.databases import Databases
from tests.providers.fake_clock import FakeClock


def _sample_spec(
    name: str = "auto-rollback",
    why: str = "Rollback when drift is high",
    model: str | None = "churn",
    hook_id: str = "hok_test1",
    hook_kind: str = "rollback",
    per_incident: int = 1,
    per_day: int = 2,
) -> RuleSpec:
    return RuleSpec(
        name=name,
        why=why,
        model=model,
        terms=[
            RuleTerm(
                check_id="drift_check",
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


@pytest.mark.asyncio
async def test_store_conforms_to_rules_protocol(databases: Databases) -> None:
    clock = FakeClock()
    rules: Rules = SqliteRules(databases, clock=clock)
    assert callable(rules.list)
    assert callable(rules.store)
    assert callable(rules.set_enabled)


@pytest.mark.asyncio
async def test_store_rule_creates_enabled_rule(databases: Databases) -> None:
    clock = FakeClock()
    store = SqliteRules(databases, clock=clock)
    project_id = "prj_test1"
    spec = _sample_spec()

    view = await store.store(project_id, spec, proposal_id="prp_001")
    assert view.id.startswith("rul_")
    assert view.enabled is True
    assert view.proposal_id == "prp_001"
    assert view.spec == spec
    assert view.approved == clock.now()


@pytest.mark.asyncio
async def test_store_is_idempotent_per_proposal(databases: Databases) -> None:
    clock = FakeClock()
    store = SqliteRules(databases, clock=clock)
    project_id = "prj_test1"
    spec1 = _sample_spec(name="rule-1")
    spec2 = _sample_spec(name="rule-2")

    view1 = await store.store(project_id, spec1, proposal_id="prp_dup")
    clock.advance(3600.0)
    view2 = await store.store(project_id, spec2, proposal_id="prp_dup")

    assert view2.id == view1.id
    assert view2.spec == spec1  # unchanged
    assert view2.approved == view1.approved


@pytest.mark.asyncio
async def test_get_rule_by_id(databases: Databases) -> None:
    clock = FakeClock()
    store = SqliteRules(databases, clock=clock)
    project_id = "prj_test1"
    spec = _sample_spec()

    created = await store.store(project_id, spec, proposal_id="prp_get")
    fetched = await store.get(project_id, created.id)
    assert fetched is not None
    assert fetched == created

    missing = await store.get(project_id, "rul_nonexistent")
    assert missing is None


@pytest.mark.asyncio
async def test_list_rules_ordered_by_approved_ascending(databases: Databases) -> None:
    clock = FakeClock()
    store = SqliteRules(databases, clock=clock)
    project_id = "prj_test1"

    spec1 = _sample_spec(name="first")
    v1 = await store.store(project_id, spec1, proposal_id="prp_1")

    clock.advance(600.0)
    spec2 = _sample_spec(name="second")
    v2 = await store.store(project_id, spec2, proposal_id="prp_2")

    listed = await store.list(project_id)
    assert len(listed) == 2
    assert listed[0].id == v1.id
    assert listed[1].id == v2.id


@pytest.mark.asyncio
async def test_list_rules_filtered_by_model(databases: Databases) -> None:
    clock = FakeClock()
    store = SqliteRules(databases, clock=clock)
    project_id = "prj_test1"

    churn_spec = _sample_spec(name="churn-rule", model="churn")
    fraud_spec = _sample_spec(name="fraud-rule", model="fraud")
    global_spec = _sample_spec(name="global-rule", model=None)

    await store.store(project_id, churn_spec, proposal_id="prp_churn")
    await store.store(project_id, fraud_spec, proposal_id="prp_fraud")
    await store.store(project_id, global_spec, proposal_id="prp_global")

    churn_rules = await store.list(project_id, model="churn")
    assert len(churn_rules) == 1
    assert churn_rules[0].spec.name == "churn-rule"

    fraud_rules = await store.list(project_id, model="fraud")
    assert len(fraud_rules) == 1
    assert fraud_rules[0].spec.name == "fraud-rule"

    all_rules = await store.list(project_id)
    assert len(all_rules) == 3


@pytest.mark.asyncio
async def test_set_enabled(databases: Databases) -> None:
    clock = FakeClock()
    store = SqliteRules(databases, clock=clock)
    project_id = "prj_test1"
    spec = _sample_spec()

    rule = await store.store(project_id, spec, proposal_id="prp_en")
    assert rule.enabled is True

    # Disable
    disabled = await store.set_enabled(project_id, rule.id, False)
    assert disabled.enabled is False

    refetched = await store.get(project_id, rule.id)
    assert refetched is not None
    assert refetched.enabled is False

    # Enable
    enabled = await store.set_enabled(project_id, rule.id, True)
    assert enabled.enabled is True

    # KeyError on missing
    with pytest.raises(KeyError):
        await store.set_enabled(project_id, "rul_unknown", False)
