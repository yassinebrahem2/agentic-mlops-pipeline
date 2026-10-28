"""Approval observers (I13): told of every approval asked and every move out of pending, in
registration order, each awaited before the service resumes or stops the turn."""

from __future__ import annotations

import asyncio
import logging

import pytest

from mlops_agents.agents.approvals.contract import ApprovalView
from mlops_agents.agents.approvals.service import ApprovalsService
from mlops_agents.agents.approvals.store import ApprovalRecord, ApprovalStore
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.tools.base import Preview, RiskClass
from mlops_agents.storage.databases import Databases
from tests.agents.approval_env import (
    STALL_PREVIEW,
    RaisingObserver,
    RecordingObserver,
    add_turn,
)
from tests.agents.test_approvals_service import FakeSessionsForApproval

PROJECT = "prj_obs"


class OrderedSessions(FakeSessionsForApproval):
    """Writes each resume and stop into the observer's call list, so their order shows."""

    def __init__(self, store: SessionStore, calls: list[tuple[str, str, str]]) -> None:
        super().__init__(store)
        self.calls = calls

    async def resume_turn(self, project_id: str, session_id: str, turn_id: str) -> None:
        await super().resume_turn(project_id, session_id, turn_id)
        self.calls.append(("resumed", turn_id, ""))

    async def stop(self, project_id: str, agent: str) -> None:
        self.calls.append(("stopped", agent, ""))


async def _tool_approval(
    store: ApprovalStore, message_id: str, turn_id: str = "trn_1", call: str = "call_1"
) -> ApprovalRecord:
    return await store.create(
        PROJECT,
        turn_id=turn_id,
        session_id="ses_1",
        message_id=message_id,
        agent="orchestrator",
        tool_call_id=call,
        tool="edit_file",
        risk_class=RiskClass.WRITE_CODE,
        preview=Preview(kind="diff", title="Edit", body="+x"),
    )


def _service(
    databases: Databases, store: ApprovalStore, calls: list[tuple[str, str, str]]
) -> tuple[ApprovalsService, OrderedSessions]:
    sessions = OrderedSessions(SessionStore(databases), calls)
    return ApprovalsService(databases, sessions, store=store), sessions


@pytest.mark.asyncio
async def test_asked_fires_once_per_create_and_once_per_stall(databases: Databases) -> None:
    message_id = await add_turn(databases, PROJECT, status="running")
    store = ApprovalStore(databases)
    observer = RecordingObserver()
    store.add_observer(observer)

    tool = await _tool_approval(store, message_id)
    stall, _ = await store.create_stall(PROJECT, "trn_1", 1, STALL_PREVIEW)
    await store.create_stall(PROJECT, "trn_1", 1, STALL_PREVIEW)

    assert observer.calls == [("asked", tool.id, "pending"), ("asked", stall.id, "pending")]


@pytest.mark.asyncio
async def test_decided_fires_once_with_the_final_view_on_every_move_out_of_pending(
    databases: Databases,
) -> None:
    approve_msg = await add_turn(databases, PROJECT, turn_id="trn_a")
    reject_msg = await add_turn(databases, PROJECT, turn_id="trn_r")
    cancel_msg = await add_turn(databases, PROJECT, turn_id="trn_c")
    stop_msg = await add_turn(databases, PROJECT, turn_id="trn_s")
    stale_msg = await add_turn(databases, PROJECT, turn_id="trn_x", status="interrupted")
    store = ApprovalStore(databases)
    to_approve = await _tool_approval(store, approve_msg, "trn_a")
    to_reject = await _tool_approval(store, reject_msg, "trn_r")
    to_cancel = await _tool_approval(store, cancel_msg, "trn_c")
    to_stop = await _tool_approval(store, stop_msg, "trn_s")
    to_expire = await _tool_approval(store, stale_msg, "trn_x")
    calls: list[tuple[str, str, str]] = []
    store.add_observer(RecordingObserver(calls))
    service, _ = _service(databases, store, calls)

    await service.decide(PROJECT, to_approve.id, approve=True)
    await service.decide(PROJECT, to_reject.id, approve=False, note="no")
    await service.cancel(PROJECT, to_cancel.id)
    await store.cancel_for_turn(PROJECT, "trn_s")
    await store.expire_non_waiting(PROJECT)

    decided = [c for c in calls if c[0] == "decided"]
    assert decided == [
        ("decided", to_approve.id, "approved"),
        ("decided", to_reject.id, "rejected"),
        ("decided", to_cancel.id, "cancelled"),
        ("decided", to_stop.id, "cancelled"),
        ("decided", to_expire.id, "expired"),
    ]


@pytest.mark.asyncio
async def test_observers_run_in_registration_order(databases: Databases) -> None:
    message_id = await add_turn(databases, PROJECT, status="running")
    store = ApprovalStore(databases)
    order: list[str] = []

    class Named:
        def __init__(self, name: str, delay: float) -> None:
            self.name = name
            self.delay = delay

        async def asked(self, view: ApprovalView) -> None:
            await asyncio.sleep(self.delay)
            order.append(self.name)

        async def decided(self, view: ApprovalView) -> None:
            order.append(self.name)

    store.add_observer(Named("slow first", 0.1))
    store.add_observer(Named("quick second", 0))

    await _tool_approval(store, message_id)

    assert order == ["slow first", "quick second"]


@pytest.mark.asyncio
async def test_a_raising_observer_is_logged_scrubbed_and_neither_stops_the_next_nor_undoes(
    databases: Databases, caplog: pytest.LogCaptureFixture
) -> None:
    message_id = await add_turn(databases, PROJECT)
    store = ApprovalStore(databases)
    raising = RaisingObserver()
    after = RecordingObserver()
    store.add_observer(raising)
    store.add_observer(after)
    service, sessions = _service(databases, store, after.calls)

    with caplog.at_level(logging.ERROR):
        approval = await _tool_approval(store, message_id)
        decided = await service.decide(PROJECT, approval.id, approve=True)

    assert raising.calls == 2
    assert [c[0] for c in after.calls] == ["asked", "decided", "resumed"]
    assert decided.status == "approved"
    stored = await store.get(PROJECT, approval.id)
    assert stored is not None and stored.status == "approved"
    assert sessions.resumed_turns == [(PROJECT, "ses_1", "trn_1")]
    assert "observer failed" in caplog.text
    assert "hunter2" not in caplog.text


@pytest.mark.asyncio
async def test_every_decided_completes_before_the_turn_resumes(databases: Databases) -> None:
    message_id = await add_turn(databases, PROJECT)
    store = ApprovalStore(databases)
    approval = await _tool_approval(store, message_id)
    calls: list[tuple[str, str, str]] = []
    store.add_observer(RecordingObserver(calls, delay=0.1))
    store.add_observer(RecordingObserver(calls, delay=0.05))
    service, _ = _service(databases, store, calls)

    await service.decide(PROJECT, approval.id, approve=True)

    assert [c[0] for c in calls] == ["decided", "decided", "resumed"]


@pytest.mark.asyncio
async def test_every_decided_completes_before_a_rejected_stall_stops_the_turn(
    databases: Databases,
) -> None:
    await add_turn(databases, PROJECT)
    store = ApprovalStore(databases)
    stall, _ = await store.create_stall(PROJECT, "trn_1", 1, STALL_PREVIEW)
    calls: list[tuple[str, str, str]] = []
    store.add_observer(RecordingObserver(calls, delay=0.1))
    service, _ = _service(databases, store, calls)

    await service.decide(PROJECT, stall.id, approve=False)

    assert calls == [("decided", stall.id, "rejected"), ("stopped", "orchestrator", "")]


@pytest.mark.asyncio
async def test_every_decided_completes_before_a_cancel_resumes_the_turn(
    databases: Databases,
) -> None:
    message_id = await add_turn(databases, PROJECT)
    store = ApprovalStore(databases)
    approval = await _tool_approval(store, message_id)
    calls: list[tuple[str, str, str]] = []
    store.add_observer(RecordingObserver(calls, delay=0.1))
    service, _ = _service(databases, store, calls)

    await service.cancel(PROJECT, approval.id)

    assert [c[0] for c in calls] == ["decided", "resumed"]


@pytest.mark.asyncio
async def test_the_service_registers_observers_on_its_store(databases: Databases) -> None:
    message_id = await add_turn(databases, PROJECT, status="running")
    store = ApprovalStore(databases)
    observer = RecordingObserver()
    service, _ = _service(databases, store, observer.calls)

    service.add_observer(observer)
    approval = await _tool_approval(store, message_id)

    assert observer.calls == [("asked", approval.id, "pending")]
