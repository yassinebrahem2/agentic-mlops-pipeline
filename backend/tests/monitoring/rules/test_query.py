"""Unit tests for RulesQueryProvider (I33)."""

from __future__ import annotations

import pytest

from mlops_agents.ids import new_id
from mlops_agents.monitoring.expr import Compare
from mlops_agents.monitoring.query import MonitoringQuery
from mlops_agents.monitoring.rules.contract import (
    RuleLimits,
    RuleSpec,
    RuleTerm,
)
from mlops_agents.monitoring.rules.query import RulesQueryProvider
from mlops_agents.monitoring.rules.store import SqliteRules
from mlops_agents.storage.databases import Databases
from tests.providers.fake_clock import FakeClock


def _make_spec(
    name: str = "r1",
    model: str | None = "churn",
) -> RuleSpec:
    return RuleSpec(
        name=name,
        why="query test",
        model=model,
        terms=[
            RuleTerm(
                check_id="chk_1",
                model=model,
                when=Compare(op="gt", field="value", value=0.5),
                window_minutes=60,
                quantifier="latest",
                min_records=1,
            )
        ],
        hook_id="hok_1",
        hook_kind="rollback",
        limits=RuleLimits(per_incident=1, per_day=2),
    )


@pytest.mark.asyncio
async def test_rules_query_provider(databases: Databases) -> None:
    clock = FakeClock()
    rules = SqliteRules(databases, clock=clock)
    project_id = new_id("prj")

    # Store 3 rules: 2 for model "churn", 1 for model "fraud"
    r1 = await rules.store(project_id, _make_spec("rule-1", "churn"), "prp_1")
    r2 = await rules.store(project_id, _make_spec("rule-2", "churn"), "prp_2")
    r3 = await rules.store(project_id, _make_spec("rule-3", "fraud"), "prp_3")

    provider = RulesQueryProvider(rules)
    assert provider.what == "rules"

    # Query all
    page = await provider.query(project_id, MonitoringQuery(what="rules"))
    assert page.total == 3
    assert len(page.rows) == 3
    assert page.truncated is False

    # Filter by model="churn"
    page_churn = await provider.query(project_id, MonitoringQuery(what="rules", model="churn"))
    assert page_churn.total == 2
    assert {r.id for r in page_churn.rows} == {r1.id, r2.id}

    # Filter by id
    page_id = await provider.query(project_id, MonitoringQuery(what="rules", id=r3.id))
    assert page_id.total == 1
    assert page_id.rows[0].id == r3.id

    # Pagination: limit=1, offset=1 on all rules
    page_paged = await provider.query(project_id, MonitoringQuery(what="rules", limit=1, offset=1))
    assert page_paged.total == 3
    assert len(page_paged.rows) == 1
    assert page_paged.rows[0].id == r2.id
    assert page_paged.truncated is True
