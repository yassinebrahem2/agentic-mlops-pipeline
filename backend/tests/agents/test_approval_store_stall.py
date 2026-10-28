"""The stall kind in the approval store (I13), and the up-front checks of a tool approval."""

from __future__ import annotations

import asyncio

import pytest

from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.tools.base import Preview, RiskClass
from mlops_agents.errors import DomainError
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import messages
from tests.agents.approval_env import STALL_PREVIEW, add_turn

PROJECT = "prj_stall"


@pytest.mark.asyncio
async def test_create_stall_yields_a_stall_record_with_no_tool_fields(
    databases: Databases,
) -> None:
    message_id = await add_turn(databases, PROJECT, status="running")
    store = ApprovalStore(databases)

    record, created = await store.create_stall(PROJECT, "trn_1", 3, STALL_PREVIEW)

    assert created is True
    assert record.kind == "stall"
    assert record.step == 3
    assert record.status == "pending"
    assert record.tool is None
    assert record.tool_call_id is None
    assert record.risk_class is None
    assert record.proposal_kind is None
    assert record.preview == STALL_PREVIEW
    # Looked up from the turn: its session, the session's agent, the turn's agent message.
    assert record.session_id == "ses_1"
    assert record.agent == "orchestrator"
    assert record.message_id == message_id


@pytest.mark.asyncio
async def test_create_stall_twice_gives_one_record(databases: Databases) -> None:
    await add_turn(databases, PROJECT, status="running")
    store = ApprovalStore(databases)

    first, first_created = await store.create_stall(PROJECT, "trn_1", 1, STALL_PREVIEW)
    second, second_created = await store.create_stall(PROJECT, "trn_1", 1, STALL_PREVIEW)

    assert (first_created, second_created) == (True, False)
    assert second.id == first.id
    assert [a.id for a in await store.pending(PROJECT)] == [first.id]


@pytest.mark.asyncio
async def test_two_concurrent_create_stall_calls_give_one_record(databases: Databases) -> None:
    await add_turn(databases, PROJECT, status="running")
    store = ApprovalStore(databases)

    (a, a_created), (b, b_created) = await asyncio.gather(
        store.create_stall(PROJECT, "trn_1", 2, STALL_PREVIEW),
        store.create_stall(PROJECT, "trn_1", 2, STALL_PREVIEW),
    )

    assert a.id == b.id
    assert sorted([a_created, b_created]) == [False, True]
    assert len(await store.pending(PROJECT)) == 1


@pytest.mark.asyncio
async def test_a_new_stall_at_the_same_step_is_allowed_after_the_first_is_decided(
    databases: Databases,
) -> None:
    await add_turn(databases, PROJECT, status="running")
    store = ApprovalStore(databases)

    first, _ = await store.create_stall(PROJECT, "trn_1", 1, STALL_PREVIEW)
    await store.decide(PROJECT, first.id, approve=True)
    second, created = await store.create_stall(PROJECT, "trn_1", 1, STALL_PREVIEW)

    assert created is True
    assert second.id != first.id
    assert second.status == "pending"


@pytest.mark.asyncio
async def test_create_stall_refuses_a_missing_turn(databases: Databases) -> None:
    await add_turn(databases, PROJECT, status="running")
    store = ApprovalStore(databases)

    with pytest.raises(DomainError) as err:
        await store.create_stall(PROJECT, "trn_missing", 1, STALL_PREVIEW)

    assert err.value.code == "turn_not_found"
    assert await store.pending(PROJECT) == []


@pytest.mark.asyncio
async def test_create_stall_refuses_a_turn_without_its_agent_message(
    databases: Databases,
) -> None:
    await add_turn(databases, PROJECT, status="running")
    engine = await databases.project(PROJECT)
    async with engine.begin() as conn:
        await conn.execute(messages.delete())
    store = ApprovalStore(databases)

    with pytest.raises(DomainError) as err:
        await store.create_stall(PROJECT, "trn_1", 1, STALL_PREVIEW)

    assert err.value.code == "message_not_found"


@pytest.mark.asyncio
async def test_pending_expiry_and_cancel_for_turn_include_a_stall(databases: Databases) -> None:
    await add_turn(databases, PROJECT, turn_id="trn_1", status="waiting")
    await add_turn(databases, PROJECT, turn_id="trn_2", status="running")
    store = ApprovalStore(databases)

    waiting_stall, _ = await store.create_stall(PROJECT, "trn_1", 1, STALL_PREVIEW)
    stale_stall, _ = await store.create_stall(PROJECT, "trn_2", 1, STALL_PREVIEW)

    assert {a.id for a in await store.pending(PROJECT)} == {waiting_stall.id, stale_stall.id}

    expired = await store.expire_non_waiting(PROJECT)
    assert [(a.id, a.kind, a.status) for a in expired] == [(stale_stall.id, "stall", "expired")]

    cancelled = await store.cancel_for_turn(PROJECT, "trn_1")
    assert [(a.id, a.kind, a.status) for a in cancelled] == [
        (waiting_stall.id, "stall", "cancelled")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["tool", "tool_call_id", "risk_class"])
async def test_a_tool_approval_without_its_tool_fields_is_refused(
    databases: Databases, missing: str
) -> None:
    message_id = await add_turn(databases, PROJECT, status="running")
    store = ApprovalStore(databases)
    fields: dict[str, str | RiskClass | None] = {
        "tool": "edit_file",
        "tool_call_id": "call_1",
        "risk_class": RiskClass.WRITE_CODE,
    }
    fields[missing] = None

    with pytest.raises(DomainError) as err:
        await store.create(
            PROJECT,
            turn_id="trn_1",
            session_id="ses_1",
            message_id=message_id,
            agent="orchestrator",
            tool_call_id=fields["tool_call_id"],
            tool=fields["tool"],
            risk_class=fields["risk_class"],
            preview=Preview(kind="text", title="t", body="b"),
        )

    assert err.value.code == "approval_tool_missing"
    assert await store.pending(PROJECT) == []


@pytest.mark.asyncio
async def test_a_tool_approval_stores_its_kind_and_proposal_kind(databases: Databases) -> None:
    message_id = await add_turn(databases, PROJECT, status="running")
    store = ApprovalStore(databases)

    record = await store.create(
        PROJECT,
        turn_id="trn_1",
        session_id="ses_1",
        message_id=message_id,
        agent="orchestrator",
        tool_call_id="call_1",
        tool="Propose",
        risk_class=RiskClass.PROPOSE,
        preview=Preview(kind="text", title="t", body="b"),
        proposal_kind="handoff",
    )

    stored = await store.get(PROJECT, record.id)
    assert stored is not None
    assert (stored.kind, stored.step, stored.proposal_kind) == ("tool", None, "handoff")
    assert stored.risk_class == RiskClass.PROPOSE
