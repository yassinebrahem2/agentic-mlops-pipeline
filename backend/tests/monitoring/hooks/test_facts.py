"""Tests for HandoffGateFacts (Task 7, #492)."""

from __future__ import annotations

import pytest

from mlops_agents.ids import new_id
from mlops_agents.monitoring.hooks.facts import HandoffGateFacts
from mlops_agents.monitoring.hooks.service import HooksService
from mlops_agents.proposals.handoffs import SqliteHandoffs
from mlops_agents.proposals.payloads import CommandSpec, HandoffPayload, ModelSpec
from tests.proposals.samples import command, model
from tests.proposals.samples import handoff as sample_handoff


def _spec(deploys: bool = False) -> CommandSpec:
    return CommandSpec.model_validate(
        command(
            kind="train",
            type="command",
            template="python train.py",
            deploys=deploys,
        )
    )


def _model_spec(
    name: str = "churn",
    promotion: str = "team_gate",
    has_prod_signal: bool = True,
) -> ModelSpec:
    m = model(
        name=name,
        promotion=promotion,
        environments={"prod": "production", "staging": "staging"},
    )
    if not has_prod_signal:
        m["production_signal"] = None
    else:
        m["production_signal"] = {"kind": "tracker_alias", "ref": "prod"}
    return ModelSpec.model_validate(m)


@pytest.mark.asyncio
async def test_gate_facts_no_hook_or_no_handoff(
    service: HooksService,
    handoffs: SqliteHandoffs,
) -> None:
    project_id = new_id("prj")
    gate_facts = HandoffGateFacts(service, handoffs)

    # 1. hook_id is None -> (None, None, None)
    assert await gate_facts.facts(project_id, None) == (None, None, None)

    # 2. Unknown hook -> (None, None, None)
    assert await gate_facts.facts(project_id, "unknown_hook") == (None, None, None)

    # 3. Hook exists, but no handoff -> (None, None, None)
    hook = await service.register(project_id, _spec(deploys=False), "train", "churn", "proposal")
    assert await gate_facts.facts(project_id, hook.id) == (None, None, None)


@pytest.mark.asyncio
async def test_gate_facts_various_model_and_promotion_cases(
    service: HooksService,
    handoffs: SqliteHandoffs,
) -> None:
    project_id = new_id("prj")
    gate_facts = HandoffGateFacts(service, handoffs)

    # Store handoff with models
    m1 = _model_spec(name="team_model", promotion="team_gate", has_prod_signal=True)
    m2 = _model_spec(name="person_model", promotion="person", has_prod_signal=True)
    m3 = _model_spec(name="no_signal_model", promotion="team_gate", has_prod_signal=False)
    m4 = _model_spec(name="plain_model", promotion="person", has_prod_signal=False)

    payload = HandoffPayload.model_validate(
        sample_handoff(models=[m1.model_dump(), m2.model_dump(), m3.model_dump(), m4.model_dump()])
    )
    await handoffs.store(project_id, "prp_facts", payload)

    # Hook for team_model (deploys=False)
    h1 = await service.register(
        project_id, _spec(deploys=False), "train-team", "team_model", "handoff"
    )
    assert await gate_facts.facts(project_id, h1.id) == (False, True, True)

    # Hook for person_model (deploys=True)
    h2 = await service.register(
        project_id, _spec(deploys=True), "deploy-person", "person_model", "handoff"
    )
    assert await gate_facts.facts(project_id, h2.id) == (True, True, False)

    # Hook for no_signal_model (deploys=False)
    h3 = await service.register(
        project_id, _spec(deploys=False), "train-nosig", "no_signal_model", "handoff"
    )
    assert await gate_facts.facts(project_id, h3.id) == (False, False, True)

    # Hook for plain_model (deploys=False)
    h4 = await service.register(
        project_id, _spec(deploys=False), "train-plain", "plain_model", "handoff"
    )
    assert await gate_facts.facts(project_id, h4.id) == (False, False, False)

    # Project-level hook (model=None)
    h_proj = await service.register(project_id, _spec(deploys=False), "train-proj", None, "handoff")
    assert await gate_facts.facts(project_id, h_proj.id) == (False, False, False)


@pytest.mark.asyncio
async def test_gate_facts_suspended_hook_returns_deploys_true(
    service: HooksService,
    handoffs: SqliteHandoffs,
) -> None:
    project_id = new_id("prj")
    gate_facts = HandoffGateFacts(service, handoffs)

    m = _model_spec(name="churn", promotion="team_gate", has_prod_signal=True)
    payload = HandoffPayload.model_validate(sample_handoff(models=[m.model_dump()]))
    await handoffs.store(project_id, "prp_susp", payload)

    hook = await service.register(project_id, _spec(deploys=False), "train", "churn", "handoff")
    assert await gate_facts.facts(project_id, hook.id) == (False, True, True)

    # Suspend hook
    await service.suspend(project_id, hook.id, "Pins changed")

    # While suspended, deploys is treated as True
    facts = await gate_facts.facts(project_id, hook.id)
    assert facts == (True, True, True)
