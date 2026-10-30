"""Tests for ProposalsService.recover and SqliteHandoffs crash recovery."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from mlops_agents.credentials.project_secrets import (
    ProjectSecrets,
    proposal_slot_ref,
)
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.proposals.contract import ApplyResult
from mlops_agents.proposals.handoffs import SqliteHandoffs
from mlops_agents.proposals.kinds import ProposalKinds
from mlops_agents.proposals.payloads import HandoffPayload
from mlops_agents.proposals.service import ProposalsService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import handoff_versions
from mlops_agents.storage.tables import proposals as proposals_table
from tests.proposals.conftest import HandoffTestKind, ScriptedTestKind
from tests.proposals.samples import connection, handoff


@pytest.mark.asyncio
async def test_recover_lifecycle_and_idempotent_reapply(
    service: ProposalsService,
    kinds: ProposalKinds,
    project_secrets: ProjectSecrets,
    databases: Databases,
    event_log: SqliteEventLog,
) -> None:
    handoffs = SqliteHandoffs(databases)
    test_conn_kind = ScriptedTestKind(name="connections")
    test_hnd_kind = HandoffTestKind(handoffs)
    kinds.register(test_conn_kind)
    kinds.register(test_hnd_kind)
    project_id = "prj_recover"
    engine = await databases.project(project_id)

    # 1. Approved proposal with no result -> applied again and swept
    v_approved = await service.create(
        project_id,
        "architect",
        test_conn_kind.name,
        "k_app",
        {"connections": [connection()]},
        "trn_1",
        "call_1",
    )
    ref_approved = proposal_slot_ref(project_id, v_approved.id, 0)
    await project_secrets.put(ref_approved, "secret_app")
    async with engine.begin() as conn:
        await conn.execute(
            proposals_table.update()
            .where(proposals_table.c.id == v_approved.id)
            .values(status="approved", result=None)
        )

    # 2. Orphan slot of a rejected proposal -> deleted
    v_rej = await service.create(
        project_id,
        "architect",
        test_conn_kind.name,
        "k_rej",
        {"connections": [connection()]},
        "trn_2",
        "call_2",
    )
    ref_rej = proposal_slot_ref(project_id, v_rej.id, 0)
    await project_secrets.put(ref_rej, "secret_orphan")
    async with engine.begin() as conn:
        await conn.execute(
            proposals_table.update()
            .where(proposals_table.c.id == v_rej.id)
            .values(status="rejected")
        )

    # 3. Pending proposal WITH approval -> slots kept
    v_pending = await service.create(
        project_id,
        "architect",
        test_conn_kind.name,
        "k_pend",
        {"connections": [connection()]},
        "trn_3",
        "call_3",
    )
    await service.attach(project_id, v_pending.id, "apr_pend")
    ref_pending = proposal_slot_ref(project_id, v_pending.id, 0)
    await project_secrets.put(ref_pending, "secret_keep")

    # 4. Pending proposal with NO approval -> marked rejected, proposal.decided emitted, and swept
    v_no_apr = await service.create(
        project_id,
        "architect",
        test_conn_kind.name,
        "k_noapr",
        {"connections": [connection()]},
        "trn_4",
        "call_4",
    )
    ref_no_apr = proposal_slot_ref(project_id, v_no_apr.id, 0)
    await project_secrets.put(ref_no_apr, "secret_noapr")

    # 5. Approved handoff proposal with no result:
    # Store handoff FIRST, simulating a crash right after handoffs.store but before proposals.update_result
    v_hnd = await service.create(
        project_id, "architect", test_hnd_kind.name, "k_hnd", handoff(), "trn_5", "call_5"
    )
    payload_hnd = HandoffPayload.model_validate(handoff())
    first_version = await handoffs.store(project_id, v_hnd.id, payload_hnd)
    assert first_version == 1

    async with engine.begin() as conn:
        await conn.execute(
            proposals_table.update()
            .where(proposals_table.c.id == v_hnd.id)
            .values(status="approved", result=None)
        )

    # 6. Approved proposal with an unknown/unregistered kind: recover should handle gracefully
    v_unknown = await service.create(
        project_id,
        "architect",
        test_conn_kind.name,
        "k_unk",
        {"connections": [connection()]},
        "trn_6",
        "call_6",
    )
    async with engine.begin() as conn:
        await conn.execute(
            proposals_table.update()
            .where(proposals_table.c.id == v_unknown.id)
            .values(kind="unregistered_kind", status="approved", result=None)
        )

    # Run recover!
    await service.recover(project_id)

    # 1 check: approved proposal was applied and swept
    assert len(test_conn_kind.apply_calls) == 1
    assert not await project_secrets.is_set(ref_approved)
    v1_check = await service.for_call(project_id, "trn_1", "call_1")
    assert v1_check is not None
    assert v1_check.result is not None

    # 2 check: orphan slot of rejected proposal was deleted
    assert not await project_secrets.is_set(ref_rej)

    # 3 check: pending proposal slots were kept!
    assert await project_secrets.is_set(ref_pending)

    # 4 check: pending proposal with no approval is rejected, swept, and emitted proposal.decided
    v4_check = await service.for_call(project_id, "trn_4", "call_4")
    assert v4_check is not None
    assert v4_check.status == "rejected"
    assert not await project_secrets.is_set(ref_no_apr)
    events_v4 = await event_log.history(project_id, types=["proposal.decided"])
    assert any(
        e.payload.get("proposal_id") == v_no_apr.id and e.payload.get("status") == "rejected"
        for e in events_v4
    )

    # 5 check: re-applying approved handoff proposal leaves exactly ONE row and version 1 in handoff_versions
    assert test_hnd_kind.apply_calls == [v_hnd.id]
    async with engine.connect() as conn:
        hnd_rows = (await conn.execute(select(handoff_versions))).all()
    assert len(hnd_rows) == 1

    curr_hnd = await handoffs.current(project_id)
    assert curr_hnd is not None
    assert curr_hnd.version == 1

    # 6 check: unknown kind was marked failed in db and did not crash recovery
    async with engine.connect() as conn:
        row6 = (
            (
                await conn.execute(
                    select(proposals_table).where(proposals_table.c.id == v_unknown.id)
                )
            )
            .mappings()
            .one()
        )
    assert row6["result"] is not None
    assert "Unknown proposal kind" in str(row6["result"])
    assert "unregistered_kind" in str(row6["result"])


@pytest.mark.asyncio
async def test_handoffs_store_idempotency_inserts_nothing_on_duplicate(
    databases: Databases,
) -> None:
    handoffs = SqliteHandoffs(databases)
    project_id = "prj_hndstore"
    engine = await databases.project(project_id)
    p = HandoffPayload.model_validate(handoff())

    v1 = await handoffs.store(project_id, "prp_same", p)
    assert v1 == 1

    async with engine.connect() as conn:
        rows_1 = (await conn.execute(select(handoff_versions))).all()
    assert len(rows_1) == 1

    v2 = await handoffs.store(project_id, "prp_same", p)
    assert v2 == 1

    async with engine.connect() as conn:
        rows_2 = (await conn.execute(select(handoff_versions))).all()
    assert len(rows_2) == 1


@pytest.mark.asyncio
async def test_recover_scrubs_failure_reasons(
    service: ProposalsService,
    kinds: ProposalKinds,
    databases: Databases,
) -> None:
    test_result_kind = ScriptedTestKind(name="connections")
    test_result_kind.apply_result = ApplyResult(
        applied=[],
        failed={"service": "Connection failed to http://user:secretpass@internal-service/api"},
    )
    kinds.register(test_result_kind)

    test_raising_kind = ScriptedTestKind(name="handoff")
    test_raising_kind.payload_model = HandoffPayload
    test_raising_kind.apply_error = RuntimeError(
        "Failed connecting to https://admin:supersecret@db.internal:5432"
    )
    kinds.register(test_raising_kind)

    project_id = "prj_recoverscrub"
    engine = await databases.project(project_id)

    v1 = await service.create(
        project_id,
        "architect",
        test_result_kind.name,
        "k_scrub_result",
        {"connections": [connection()]},
        "trn_sc1",
        "call_sc1",
    )
    async with engine.begin() as conn:
        await conn.execute(
            proposals_table.update()
            .where(proposals_table.c.id == v1.id)
            .values(status="approved", result=None)
        )

    v2 = await service.create(
        project_id,
        "architect",
        test_raising_kind.name,
        "k_scrub_exc",
        handoff(),
        "trn_sc2",
        "call_sc2",
    )
    async with engine.begin() as conn:
        await conn.execute(
            proposals_table.update()
            .where(proposals_table.c.id == v2.id)
            .values(status="approved", result=None)
        )

    await service.recover(project_id)

    async with engine.connect() as conn:
        row1 = (
            (await conn.execute(select(proposals_table).where(proposals_table.c.id == v1.id)))
            .mappings()
            .one()
        )
        row2 = (
            (await conn.execute(select(proposals_table).where(proposals_table.c.id == v2.id)))
            .mappings()
            .one()
        )

    assert "user:secretpass@" not in str(row1["result"])
    assert "***@" in str(row1["result"])
    assert "admin:supersecret@" not in str(row2["result"])
    assert "***@" in str(row2["result"])
