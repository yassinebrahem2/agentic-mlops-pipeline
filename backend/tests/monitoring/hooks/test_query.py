"""Tests for HooksQueryProvider (I33)."""

from __future__ import annotations

import pytest

from mlops_agents.ids import new_id
from mlops_agents.monitoring.hooks.query import HooksQueryProvider
from mlops_agents.monitoring.hooks.service import HooksService
from mlops_agents.monitoring.query import MonitoringQuery, QueryProviders
from mlops_agents.proposals.payloads import CommandSpec
from tests.proposals.samples import command


def _spec(kind: str = "train") -> CommandSpec:
    return CommandSpec.model_validate(
        command(
            kind=kind,
            type="command",
            template="echo 1",
            placeholders=[],
        )
    )


@pytest.mark.asyncio
async def test_hooks_query_provider_what(service: HooksService) -> None:
    provider = HooksQueryProvider(service)
    assert provider.what == "hooks"


@pytest.mark.asyncio
async def test_hooks_query_provider_registered_with_registry(
    service: HooksService,
) -> None:
    registry = QueryProviders()
    provider = HooksQueryProvider(service)
    registry.register(provider)
    assert registry.get("hooks") is provider

    with pytest.raises(ValueError, match="already registered"):
        registry.register(provider)


@pytest.mark.asyncio
async def test_hooks_query_pages_and_filters_by_model(
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    provider = HooksQueryProvider(service)

    # Register hooks for model "churn", "fraud", and project-level (None)
    h_churn1 = await service.register(project_id, _spec("train"), "train_churn", "churn", "handoff")
    h_churn2 = await service.register(
        project_id, _spec("evaluate"), "eval_churn", "churn", "handoff"
    )
    h_fraud = await service.register(project_id, _spec("train"), "train_fraud", "fraud", "handoff")
    await service.register(project_id, _spec("hold"), "hold_global", None, "handoff")

    # 1. Filter by model "churn"
    q_churn = MonitoringQuery(what="hooks", model="churn")
    page_churn = await provider.query(project_id, q_churn)
    assert page_churn.total == 2
    assert {h.id for h in page_churn.rows} == {h_churn1.id, h_churn2.id}
    assert page_churn.truncated is False

    # 2. Filter by model "fraud"
    q_fraud = MonitoringQuery(what="hooks", model="fraud")
    page_fraud = await provider.query(project_id, q_fraud)
    assert page_fraud.total == 1
    assert page_fraud.rows[0].id == h_fraud.id

    # 3. Query all (no model filter)
    q_all = MonitoringQuery(what="hooks")
    page_all = await provider.query(project_id, q_all)
    assert page_all.total == 4
    assert len(page_all.rows) == 4

    # 4. Filter by id
    q_id = MonitoringQuery(what="hooks", id=h_churn1.id)
    page_id = await provider.query(project_id, q_id)
    assert page_id.total == 1
    assert page_id.rows[0].id == h_churn1.id

    # 5. Filter by status
    await service.set_enabled(project_id, h_churn2.id, False)
    q_disabled = MonitoringQuery(what="hooks", status="disabled")
    page_disabled = await provider.query(project_id, q_disabled)
    assert page_disabled.total == 1
    assert page_disabled.rows[0].id == h_churn2.id

    # 6. Pagination (offset and limit)
    q_page1 = MonitoringQuery(what="hooks", limit=2, offset=0)
    page1 = await provider.query(project_id, q_page1)
    assert page1.total == 4
    assert len(page1.rows) == 2
    assert page1.truncated is True

    q_page2 = MonitoringQuery(what="hooks", limit=2, offset=2)
    page2 = await provider.query(project_id, q_page2)
    assert page2.total == 4
    assert len(page2.rows) == 2
    assert page2.truncated is False

    # Check distinct rows across pages
    ids_page1 = {r.id for r in page1.rows}
    ids_page2 = {r.id for r in page2.rows}
    assert ids_page1.isdisjoint(ids_page2)
