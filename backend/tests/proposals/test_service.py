"""Tests for ProposalsService, ProposalStore, and recover."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr
from sqlalchemy import select

from mlops_agents.agents.tools.base import Preview
from mlops_agents.credentials.project_secrets import (
    ProjectSecrets,
    proposal_slot_ref,
)
from mlops_agents.errors import DomainError
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.proposals.contract import (
    HANDOFF_KIND,
    ApplyResult,
    CredentialSlot,
    HandoffPayload,
)
from mlops_agents.proposals.handoffs import SqliteHandoffs
from mlops_agents.proposals.kinds import ProposalKinds
from mlops_agents.proposals.payloads import ConnectionsPayload
from mlops_agents.proposals.service import ProposalsService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import proposals as proposals_table
from tests.conftest import MemoryKeyStore
from tests.proposals.samples import connection, handoff


class ScriptedTestKind:
    payload_model: type[BaseModel] = ConnectionsPayload

    def __init__(
        self,
        name: str = "connections",
        slots: list[CredentialSlot] | None = None,
        warnings_list: list[str] | None = None,
    ) -> None:
        self.name = name
        self._slots = (
            slots
            if slots is not None
            else [
                CredentialSlot(index=0, label="Key 0", source_type="token"),
                CredentialSlot(index=1, label="Key 1", source_type="password"),
            ]
        )
        self._warnings = warnings_list or ["Test warning line 1"]
        self.apply_calls: list[tuple[str, str, Any]] = []
        self.apply_error: Exception | None = None
        self.apply_result: ApplyResult | None = None
        self.on_apply_coro: Any = None

    def key(self, payload: BaseModel) -> str:
        return "const_key"

    def preview(self, payload: BaseModel) -> Preview:
        return Preview(kind="text", title="Test Preview", body="preview text")

    def warnings(self, payload: BaseModel) -> list[str]:
        return list(self._warnings)

    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]:
        return list(self._slots)

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        self.apply_calls.append((project_id, proposal_id, payload))
        if self.on_apply_coro is not None:
            return await self.on_apply_coro(project_id, proposal_id, payload)
        if self.apply_error is not None:
            raise self.apply_error
        if self.apply_result is not None:
            return self.apply_result
        return ApplyResult(applied=["step_applied"], failed={})


class HandoffTestKind:
    payload_model: type[BaseModel] = HandoffPayload

    def __init__(self, handoffs: SqliteHandoffs) -> None:
        self.name = HANDOFF_KIND
        self._handoffs = handoffs
        self.apply_calls: list[str] = []

    def key(self, payload: BaseModel) -> str:
        return "handoff_key"

    def preview(self, payload: BaseModel) -> Preview:
        return Preview(kind="text", title="Handoff", body="handoff preview")

    def warnings(self, payload: BaseModel) -> list[str]:
        return []

    def credentials_needed(self, payload: BaseModel) -> list[CredentialSlot]:
        return []

    async def apply(self, project_id: str, proposal_id: str, payload: BaseModel) -> ApplyResult:
        self.apply_calls.append(proposal_id)
        assert isinstance(payload, HandoffPayload)
        v = await self._handoffs.store(project_id, proposal_id, payload)
        return ApplyResult(applied=[f"version_{v}"], failed={})


@pytest.fixture
def memory_keystore() -> MemoryKeyStore:
    return MemoryKeyStore()


@pytest.fixture
def project_secrets(memory_keystore: MemoryKeyStore, databases: Databases) -> ProjectSecrets:
    return ProjectSecrets(memory_keystore, databases)


@pytest.fixture
def event_log(databases: Databases) -> SqliteEventLog:
    return SqliteEventLog(databases)


@pytest.fixture
def kinds() -> ProposalKinds:
    return ProposalKinds()


@pytest.fixture
def service(
    databases: Databases,
    project_secrets: ProjectSecrets,
    kinds: ProposalKinds,
    event_log: SqliteEventLog,
) -> ProposalsService:
    return ProposalsService(databases, project_secrets, kinds, event_log)


def test_payload_models_have_no_secret_fields() -> None:
    def check_model(model_cls: type[BaseModel], visited: set[type[BaseModel]]) -> None:
        if model_cls in visited:
            return
        visited.add(model_cls)
        for field_name, field_info in model_cls.model_fields.items():
            assert field_info.annotation is not SecretStr, f"{model_cls}.{field_name} is SecretStr"
            lower = field_name.lower()
            assert "secret" not in lower, f"{model_cls} has secret field {field_name}"
            assert "password" not in lower, f"{model_cls} has password field {field_name}"
            annotation = field_info.annotation
            # Check nested models
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                check_model(annotation, visited)

    check_model(ConnectionsPayload, set())
    check_model(HandoffPayload, set())


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
async def test_create_duplicate_in_same_turn_raises_and_stores_nothing(
    service: ProposalsService,
    kinds: ProposalKinds,
) -> None:
    test_kind = ScriptedTestKind()
    kinds.register(test_kind)
    project_id = "prj_1"
    valid_payload = {"connections": [connection()]}

    await service.create(
        project_id, "architect", test_kind.name, "key1", valid_payload, "trn_1", "call_1"
    )

    with pytest.raises(DomainError) as exc_info:
        await service.create(
            project_id, "architect", test_kind.name, "key1", valid_payload, "trn_1", "call_2"
        )
    assert exc_info.value.code == "proposal_duplicate_in_step"


@pytest.mark.asyncio
async def test_create_invalid_payload_raises_422_and_stores_nothing(
    service: ProposalsService,
    kinds: ProposalKinds,
    databases: Databases,
) -> None:
    test_kind = ScriptedTestKind()
    kinds.register(test_kind)
    project_id = "prj_1"

    with pytest.raises(DomainError) as exc_info:
        await service.create(
            project_id, "architect", test_kind.name, "key1", {"connections": []}, "trn_1", "call_1"
        )
    assert exc_info.value.status == 422

    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (await conn.execute(select(proposals_table))).all()
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_attach_and_superseding(
    service: ProposalsService,
    kinds: ProposalKinds,
    project_secrets: ProjectSecrets,
) -> None:
    test_kind = ScriptedTestKind()
    kinds.register(test_kind)
    project_id = "prj_1"
    valid_payload = {"connections": [connection()]}

    # Create draft 1
    v1 = await service.create(
        project_id, "architect", test_kind.name, "key_a", valid_payload, "trn_1", "call_1"
    )
    # Populate slot in draft 1
    ref_0 = proposal_slot_ref(project_id, v1.id, 0)
    await project_secrets.put(ref_0, "secret0")

    # Attach draft 1
    old_apr = await service.attach(project_id, v1.id, "apr_1")
    assert old_apr is None
    v1_loaded = await service.get_by_approval(project_id, "apr_1")
    assert v1_loaded is not None
    assert v1_loaded.id == v1.id

    # Create draft 2 with DIFFERENT key -> attach should leave draft 1 alone
    v2_diff = await service.create(
        project_id, "architect", test_kind.name, "key_diff", valid_payload, "trn_2", "call_diff"
    )
    assert await service.attach(project_id, v2_diff.id, "apr_diff") is None

    # Create draft 3 with SAME key -> attach supersedes draft 1, sweeps its slots, returns apr_1
    v3 = await service.create(
        project_id, "architect", test_kind.name, "key_a", valid_payload, "trn_3", "call_3"
    )
    superseded_apr = await service.attach(project_id, v3.id, "apr_3")
    assert superseded_apr == "apr_1"

    # v1 is now superseded and its slots swept
    v1_check = await service.get_by_approval(project_id, "apr_1")
    assert v1_check is not None
    assert v1_check.status == "superseded"
    assert not await project_secrets.is_set(ref_0)


@pytest.mark.asyncio
async def test_for_call_and_latest_for_turn(
    service: ProposalsService,
    kinds: ProposalKinds,
) -> None:
    test_kind = ScriptedTestKind()
    kinds.register(test_kind)
    project_id = "prj_1"
    valid_payload = {"connections": [connection()]}

    v1 = await service.create(
        project_id, "architect", test_kind.name, "k1", valid_payload, "trn_1", "call_1"
    )
    found = await service.for_call(project_id, "trn_1", "call_1")
    assert found is not None
    assert found.id == v1.id

    assert await service.for_call(project_id, "trn_1", "nonexistent") is None

    latest = await service.latest_for_turn(project_id, "trn_1", test_kind.name)
    assert latest is not None
    assert latest.id == v1.id


@pytest.mark.asyncio
async def test_decide_approval_and_rejection(
    service: ProposalsService,
    kinds: ProposalKinds,
    project_secrets: ProjectSecrets,
    event_log: SqliteEventLog,
) -> None:
    test_kind = ScriptedTestKind()
    kinds.register(test_kind)
    project_id = "prj_1"
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
    project_id = "prj_1"
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
async def test_apply_failure_handling_and_sweep(
    service: ProposalsService,
    kinds: ProposalKinds,
    project_secrets: ProjectSecrets,
    memory_keystore: MemoryKeyStore,
) -> None:
    # Test apply that moves slot 0 and fails on slot 1
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
    # The destination secret still exists
    assert memory_keystore.get(f"project/{project_id}/permanent") == "secret0"


@pytest.mark.asyncio
async def test_apply_raising_stores_failed_reason_and_sweeps(
    service: ProposalsService,
    kinds: ProposalKinds,
    project_secrets: ProjectSecrets,
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


@pytest.mark.asyncio
async def test_warnings_and_db_row(
    service: ProposalsService,
    kinds: ProposalKinds,
    databases: Databases,
) -> None:
    test_kind = ScriptedTestKind(warnings_list=["A custom warning message"])
    kinds.register(test_kind)
    project_id = "prj_1"

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

    # In database row, payload and row do not store the warnings text
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        row = (
            (await conn.execute(select(proposals_table).where(proposals_table.c.id == v.id)))
            .mappings()
            .one()
        )
    assert "A custom warning message" not in row["payload"]


@pytest.mark.asyncio
async def test_recover(
    service: ProposalsService,
    kinds: ProposalKinds,
    project_secrets: ProjectSecrets,
    databases: Databases,
) -> None:
    handoffs = SqliteHandoffs(databases)
    test_conn_kind = ScriptedTestKind(name="connections")
    test_hnd_kind = HandoffTestKind(handoffs)
    kinds.register(test_conn_kind)
    kinds.register(test_hnd_kind)
    project_id = "prj_recover"

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
    # Manually set status to approved with result NULL in db
    engine = await databases.project(project_id)
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

    # 4. Pending proposal with NO approval -> marked rejected and swept
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

    # 5. Approved handoff proposal with no result -> re-applied, leaves one version
    v_hnd = await service.create(
        project_id, "architect", test_hnd_kind.name, "k_hnd", handoff(), "trn_5", "call_5"
    )
    async with engine.begin() as conn:
        await conn.execute(
            proposals_table.update()
            .where(proposals_table.c.id == v_hnd.id)
            .values(status="approved", result=None)
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

    # 4 check: pending proposal with no approval is rejected and swept
    v4_check = await service.for_call(project_id, "trn_4", "call_4")
    assert v4_check is not None
    assert v4_check.status == "rejected"
    assert not await project_secrets.is_set(ref_no_apr)

    # 5 check: re-applying approved handoff proposal leaves exactly one version
    curr_hnd = await handoffs.current(project_id)
    assert curr_hnd is not None
    assert curr_hnd.version == 1
