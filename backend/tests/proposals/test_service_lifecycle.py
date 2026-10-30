"""Tests for proposal service lifecycle: creation, events, attaching, superseding, and decisions."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from mlops_agents.credentials.project_secrets import (
    ProjectSecrets,
    proposal_slot_ref,
)
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.proposals.kinds import ProposalKinds
from mlops_agents.proposals.service import ProposalsService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import proposals as proposals_table
from tests.proposals.conftest import ScriptedTestKind
from tests.proposals.samples import connection


@pytest.mark.asyncio
async def test_create_stores_draft_and_emits_event_without_payload(
    service: ProposalsService,
    kinds: ProposalKinds,
    event_log: SqliteEventLog,
) -> None:
    test_kind = ScriptedTestKind()
    kinds.register(test_kind)
    project_id = "prj_1"
    valid_payload = {"connections": [connection()]}

    view = await service.create(
        project_id,
        agent="architect",
        kind=test_kind.name,
        key="key1",
        payload=valid_payload,
        turn_id="trn_1",
        tool_call_id="call_1",
    )

    assert view.status == "pending"
    assert view.approval_id is None
    assert view.agent == "architect"
    assert view.turn_id == "trn_1"
    assert view.tool_call_id == "call_1"
    assert view.key == "key1"

    # Event was emitted with proposal_id and kind, but without payload
    events = await event_log.history(project_id, types=["proposal.created"])
    assert len(events) == 1
    assert events[0].type == "proposal.created"
    assert events[0].payload == {"proposal_id": view.id, "kind": test_kind.name}
    assert "payload" not in events[0].payload


@pytest.mark.asyncio
async def test_create_idempotent_by_turn_and_tool_call_id(
    service: ProposalsService,
    kinds: ProposalKinds,
    event_log: SqliteEventLog,
) -> None:
    test_kind = ScriptedTestKind()
    kinds.register(test_kind)
    project_id = "prj_1"
    valid_payload = {"connections": [connection()]}

    v1 = await service.create(
        project_id, "architect", test_kind.name, "key1", valid_payload, "trn_1", "call_1"
    )
    v2 = await service.create(
        project_id, "architect", test_kind.name, "key1", valid_payload, "trn_1", "call_1"
    )

    assert v1.id == v2.id
    events = await event_log.history(project_id, types=["proposal.created"])
    assert len(events) == 1


@pytest.mark.asyncio
async def test_attach_and_superseding(
    service: ProposalsService,
    kinds: ProposalKinds,
    project_secrets: ProjectSecrets,
) -> None:
    kind1 = ScriptedTestKind(name="kind_1")
    kind2 = ScriptedTestKind(name="kind_2")
    kinds.register(kind1)
    kinds.register(kind2)
    project_id = "prj_attach"
    valid_payload = {"connections": [connection()]}

    # Create draft 1 (kind 1, key_a)
    v1 = await service.create(
        project_id, "architect", kind1.name, "key_a", valid_payload, "trn_1", "call_1"
    )
    ref_0 = proposal_slot_ref(project_id, v1.id, 0)
    await project_secrets.put(ref_0, "secret0")

    # Attach draft 1
    assert await service.attach(project_id, v1.id, "apr_1") is None
    v1_loaded = await service.get_by_approval(project_id, "apr_1")
    assert v1_loaded is not None
    assert v1_loaded.id == v1.id

    # Create draft 2 with DIFFERENT kind (kind 2, same key_a) -> leaves draft 1 untouched
    v2_diff_kind = await service.create(
        project_id, "architect", kind2.name, "key_a", valid_payload, "trn_2", "call_diff_kind"
    )
    assert await service.attach(project_id, v2_diff_kind.id, "apr_2") is None
    v1_check = await service.get_by_approval(project_id, "apr_1")
    assert v1_check is not None
    assert v1_check.status == "pending"
    assert await project_secrets.is_set(ref_0)
    v2_check = await service.get_by_approval(project_id, "apr_2")
    assert v2_check is not None
    assert v2_check.status == "pending"
    assert v2_check.kind == kind2.name

    # Create draft 3 with same kind but DIFFERENT key -> leaves draft 1 untouched
    v3_diff_key = await service.create(
        project_id, "architect", kind1.name, "key_diff", valid_payload, "trn_3", "call_diff_key"
    )
    assert await service.attach(project_id, v3_diff_key.id, "apr_3") is None

    # Create draft 4 with SAME kind and SAME key -> supersedes draft 1, sweeps slots, returns apr_1
    v4 = await service.create(
        project_id, "architect", kind1.name, "key_a", valid_payload, "trn_4", "call_4"
    )
    superseded_apr = await service.attach(project_id, v4.id, "apr_4")
    assert superseded_apr == "apr_1"

    v1_superseded = await service.get_by_approval(project_id, "apr_1")
    assert v1_superseded is not None
    assert v1_superseded.status == "superseded"
    assert not await project_secrets.is_set(ref_0)

    # v2_diff_kind (kind 2) is still pending
    v2_still_pending = await service.get_by_approval(project_id, "apr_2")
    assert v2_still_pending is not None
    assert v2_still_pending.status == "pending"


@pytest.mark.asyncio
async def test_for_call_and_latest_for_turn_across_turns_and_kinds(
    service: ProposalsService,
    kinds: ProposalKinds,
) -> None:
    kind1 = ScriptedTestKind(name="kind_alpha")
    kind2 = ScriptedTestKind(name="kind_beta")
    kinds.register(kind1)
    kinds.register(kind2)
    project_id = "prj_latest"
    valid_payload = {"connections": [connection()]}

    # Turn 1: proposal of kind1 and proposal of kind2
    t1_k1 = await service.create(
        project_id, "architect", kind1.name, "k1", valid_payload, "trn_1", "call_1_1"
    )
    t1_k2 = await service.create(
        project_id, "architect", kind2.name, "k2", valid_payload, "trn_1", "call_1_2"
    )

    # Turn 2: two proposals of kind1 (first key_2a, then key_2b)
    _t2_k1_first = await service.create(
        project_id, "architect", kind1.name, "k2a", valid_payload, "trn_2", "call_2_1"
    )
    t2_k1_second = await service.create(
        project_id, "architect", kind1.name, "k2b", valid_payload, "trn_2", "call_2_2"
    )

    # Verify for_call
    found_call = await service.for_call(project_id, "trn_1", "call_1_1")
    assert found_call is not None
    assert found_call.id == t1_k1.id
    assert await service.for_call(project_id, "trn_1", "nonexistent") is None

    # Verify latest_for_turn
    latest_t1_k1 = await service.latest_for_turn(project_id, "trn_1", kind1.name)
    assert latest_t1_k1 is not None
    assert latest_t1_k1.id == t1_k1.id

    latest_t1_k2 = await service.latest_for_turn(project_id, "trn_1", kind2.name)
    assert latest_t1_k2 is not None
    assert latest_t1_k2.id == t1_k2.id

    latest_t2_k1 = await service.latest_for_turn(project_id, "trn_2", kind1.name)
    assert latest_t2_k1 is not None
    assert latest_t2_k1.id == t2_k1_second.id

    assert await service.latest_for_turn(project_id, "trn_2", kind2.name) is None


@pytest.mark.asyncio
async def test_decide_approval_and_rejection(
    service: ProposalsService,
    kinds: ProposalKinds,
    project_secrets: ProjectSecrets,
    event_log: SqliteEventLog,
) -> None:
    test_kind = ScriptedTestKind()
    kinds.register(test_kind)
    project_id = "prj_decide"
    valid_payload = {"connections": [connection()]}

    # Test rejection
    v_rej = await service.create(
        project_id, "architect", test_kind.name, "k_rej", valid_payload, "trn_rej", "call_rej"
    )
    ref_rej = proposal_slot_ref(project_id, v_rej.id, 0)
    await project_secrets.put(ref_rej, "val_rej")

    res_rej = await service.decide(project_id, v_rej.id, approved=False)
    assert res_rej is None
    assert not await project_secrets.is_set(ref_rej)

    events_rej = await event_log.history(project_id, types=["proposal.decided"])
    assert any(e.payload.get("status") == "rejected" for e in events_rej)

    # Test approval
    v_app = await service.create(
        project_id, "architect", test_kind.name, "k_app", valid_payload, "trn_app", "call_app"
    )
    ref_app = proposal_slot_ref(project_id, v_app.id, 0)
    await project_secrets.put(ref_app, "val_app")

    res_app = await service.decide(project_id, v_app.id, approved=True)
    assert res_app is not None
    assert res_app.applied == ["step_applied"]
    assert len(test_kind.apply_calls) == 1

    # Second decide on approved proposal returns None and does not apply again
    res_app2 = await service.decide(project_id, v_app.id, approved=True)
    assert res_app2 is None
    assert len(test_kind.apply_calls) == 1

    # Slots were swept
    assert not await project_secrets.is_set(ref_app)


@pytest.mark.asyncio
async def test_decide_concurrent_applies_once(
    service: ProposalsService,
    kinds: ProposalKinds,
) -> None:
    test_kind = ScriptedTestKind()
    kinds.register(test_kind)
    project_id = "prj_conc"
    valid_payload = {"connections": [connection()]}

    v = await service.create(
        project_id, "architect", test_kind.name, "k_conc", valid_payload, "trn_c", "call_c"
    )

    r1, r2 = await asyncio.gather(
        service.decide(project_id, v.id, approved=True),
        service.decide(project_id, v.id, approved=True),
    )
    results = [r for r in (r1, r2) if r is not None]
    assert len(results) == 1
    assert len(test_kind.apply_calls) == 1


@pytest.mark.asyncio
async def test_warnings_and_db_row(
    service: ProposalsService,
    kinds: ProposalKinds,
    databases: Databases,
) -> None:
    test_kind = ScriptedTestKind(warnings_list=["A custom warning message"])
    kinds.register(test_kind)
    project_id = "prj_warn"

    v = await service.create(
        project_id,
        "architect",
        test_kind.name,
        "k_warn",
        {"connections": [connection()]},
        "trn_w",
        "call_w",
    )
    await service.attach(project_id, v.id, "apr_w")

    view = await service.get_by_approval(project_id, "apr_w")
    assert view is not None
    assert view.warnings == ["A custom warning message"]

    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        row = (
            (await conn.execute(select(proposals_table).where(proposals_table.c.id == v.id)))
            .mappings()
            .one()
        )
    assert "A custom warning message" not in row["payload"]
