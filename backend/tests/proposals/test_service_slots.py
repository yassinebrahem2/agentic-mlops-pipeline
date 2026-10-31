"""Tests for proposal credential slots, scrubbing of failed reasons, and race conditions."""

from __future__ import annotations

import asyncio
import logging

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from mlops_agents.credentials.project_secrets import (
    ProjectSecrets,
    proposal_slot_ref,
)
from mlops_agents.errors import DomainError
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.proposals.contract import ApplyResult, ProposalKind
from mlops_agents.proposals.kinds import ProposalKinds
from mlops_agents.proposals.service import ProposalsService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import events as events_table
from mlops_agents.storage.tables import proposals as proposals_table
from tests.conftest import MemoryKeyStore
from tests.proposals.conftest import ScriptedTestKind
from tests.proposals.samples import connection


class MovingTestKind(ScriptedTestKind):
    def __init__(self, secrets: ProjectSecrets, name: str = "connections") -> None:
        super().__init__(name=name)
        self._secrets = secrets

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        self.apply_calls.append((project_id, proposal_id, payload))
        src = proposal_slot_ref(project_id, proposal_id, 0)
        dst = f"project/{project_id}/permanent_ref"
        if await self._secrets.is_set(src):
            await self._secrets.move(src, dst)
        return ApplyResult(applied=["slot0_moved"], failed={})


@pytest.mark.asyncio
async def test_put_credential_and_secret_absence(
    service: ProposalsService,
    kinds: ProposalKinds,
    project_secrets: ProjectSecrets,
    databases: Databases,
    event_log: SqliteEventLog,
    caplog: pytest.LogCaptureFixture,
) -> None:
    test_kind = ScriptedTestKind()
    kinds.register(test_kind)
    project_id = "prj_absence"
    valid_payload = {"connections": [connection()]}

    v = await service.create(
        project_id, "architect", test_kind.name, "k_sec", valid_payload, "trn_1", "call_1"
    )

    secret_val = "very-secret-token-abcxyz"
    ref_0 = proposal_slot_ref(project_id, v.id, 0)

    with caplog.at_level(logging.DEBUG):
        slot_state = await service.put_credential(project_id, v.id, 0, secret_val)

    assert slot_state.index == 0
    assert slot_state.set is True
    assert await project_secrets.is_set(ref_0)

    # Secret absence checks:
    # 1. Logs
    assert secret_val not in caplog.text

    # 2. Database proposals row (payload and result)
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        row = (
            (await conn.execute(select(proposals_table).where(proposals_table.c.id == v.id)))
            .mappings()
            .one()
        )
    assert secret_val not in str(row["payload"])
    if row["result"] is not None:
        assert secret_val not in str(row["result"])

    # 3. Event log history (scan all event types in database)
    async with engine.connect() as conn:
        all_events = (await conn.execute(select(events_table))).mappings().all()
    assert len(all_events) > 0
    for ev in all_events:
        assert secret_val not in str(ev["payload"])
        assert secret_val not in str(ev)


@pytest.mark.asyncio
@pytest.mark.parametrize("moves_slot", [False, True])
async def test_put_credential_racing_with_decide(
    service: ProposalsService,
    kinds: ProposalKinds,
    project_secrets: ProjectSecrets,
    memory_keystore: MemoryKeyStore,
    moves_slot: bool,
) -> None:
    test_kind: ProposalKind = (
        MovingTestKind(project_secrets) if moves_slot else ScriptedTestKind(name="connections")
    )
    kinds.register(test_kind)
    project_id = "prj_racesvc"
    valid_payload = {"connections": [connection()]}

    v = await service.create(
        project_id, "architect", test_kind.name, "k_race", valid_payload, "trn_race", "call_race"
    )

    secret_val = "concurrent_secret_value"
    ref_0 = proposal_slot_ref(project_id, v.id, 0)
    dst_ref = f"project/{project_id}/permanent_ref"

    async def do_put() -> bool:
        try:
            await service.put_credential(project_id, v.id, 0, secret_val)
            return True
        except DomainError as err:
            assert err.code == "proposal_not_pending"
            assert err.status == 409
            return False

    async def do_decide() -> ApplyResult | None:
        return await service.decide(project_id, v.id, approved=True)

    put_ok, _decide_res = await asyncio.gather(do_put(), do_decide())

    # Proposal slot is always swept or moved, never left in slot ref
    assert not await project_secrets.is_set(ref_0)
    assert memory_keystore.get(ref_0) is None

    if put_ok:
        # PUT succeeded before decide
        if moves_slot:
            assert memory_keystore.get(dst_ref) == secret_val
        else:
            assert memory_keystore.get(dst_ref) is None
    else:
        # Decide ran first, PUT got 409
        if moves_slot:
            assert memory_keystore.get(dst_ref) is None


@pytest.mark.asyncio
async def test_apply_failure_handling_and_sweep(
    service: ProposalsService,
    kinds: ProposalKinds,
    project_secrets: ProjectSecrets,
    memory_keystore: MemoryKeyStore,
) -> None:
    async def scripted_apply(project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        src = proposal_slot_ref(project_id, proposal_id, 0)
        dst = f"project/{project_id}/permanent"
        await project_secrets.move(src, dst)
        return ApplyResult(applied=["slot0"], failed={"slot1": "unreachable"})

    test_kind = ScriptedTestKind()
    test_kind.on_apply_coro = scripted_apply
    kinds.register(test_kind)
    project_id = "prj_1"

    v = await service.create(
        project_id,
        "architect",
        test_kind.name,
        "k_move",
        {"connections": [connection()]},
        "trn_m",
        "call_m",
    )
    s0 = proposal_slot_ref(project_id, v.id, 0)
    s1 = proposal_slot_ref(project_id, v.id, 1)
    await project_secrets.put(s0, "secret0")
    await project_secrets.put(s1, "secret1")

    res = await service.decide(project_id, v.id, approved=True)
    assert res is not None
    assert res.applied == ["slot0"]
    assert "slot1" in res.failed

    # Both proposal slots should be empty now (s0 moved, s1 swept)
    assert not await project_secrets.is_set(s0)
    assert not await project_secrets.is_set(s1)
    # Direct keystore checks
    assert memory_keystore.get(s0) is None
    assert memory_keystore.get(s1) is None
    # Destination secret still exists
    assert memory_keystore.get(f"project/{project_id}/permanent") == "secret0"


@pytest.mark.asyncio
async def test_apply_raising_stores_failed_reason_and_sweeps(
    service: ProposalsService,
    kinds: ProposalKinds,
    project_secrets: ProjectSecrets,
    memory_keystore: MemoryKeyStore,
) -> None:
    test_kind = ScriptedTestKind()
    test_kind.apply_error = ValueError("Connection timeout to http://user:secret@example.com")
    kinds.register(test_kind)
    project_id = "prj_1"

    v = await service.create(
        project_id,
        "architect",
        test_kind.name,
        "k_raise",
        {"connections": [connection()]},
        "trn_r",
        "call_r",
    )
    s0 = proposal_slot_ref(project_id, v.id, 0)
    await project_secrets.put(s0, "secret0")

    res = await service.decide(project_id, v.id, approved=True)
    assert res is not None
    assert res.applied == []
    assert "apply" in res.failed
    # The error was scrubbed
    assert "secret@" not in res.failed["apply"]
    assert "***@" in res.failed["apply"]
    # Slots were swept
    assert not await project_secrets.is_set(s0)
    assert memory_keystore.get(s0) is None


@pytest.mark.asyncio
async def test_apply_returns_failed_url_credentials_scrubbed(
    service: ProposalsService,
    kinds: ProposalKinds,
    databases: Databases,
) -> None:
    raw_reason = "Connection failed to http://user:pass@host/api"
    test_kind = ScriptedTestKind()
    test_kind.apply_result = ApplyResult(applied=[], failed={"service": raw_reason})
    kinds.register(test_kind)
    project_id = "prj_scrubfailed"

    v = await service.create(
        project_id,
        "architect",
        test_kind.name,
        "k_scrub",
        {"connections": [connection()]},
        "trn_s",
        "call_s",
    )

    res = await service.decide(project_id, v.id, approved=True)
    assert res is not None
    assert "user:pass@" not in res.failed["service"]
    assert "***@" in res.failed["service"]
    assert res.failed["service"] == "Connection failed to http://***@host/api"

    # Assert stored result in database was scrubbed
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        row = (
            (await conn.execute(select(proposals_table).where(proposals_table.c.id == v.id)))
            .mappings()
            .one()
        )
    assert "user:pass@" not in str(row["result"])
    assert "***@" in str(row["result"])
