"""Tests for approval expiry at recovery and stop on waiting turns."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path

import httpx2
import pytest
from langgraph.types import interrupt
from sqlalchemy import select

from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.contract import (
    ApprovalPart,
    StreamEvent,
    TextPart,
    TurnEnd,
    TurnEnded,
    TurnWaiting,
)
from mlops_agents.agents.runtime.graph import turn_checkpointer
from mlops_agents.agents.runtime.hooks import (
    Gate,
    GateCall,
    GateDecision,
    TurnObserver,
)
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.tools.base import Preview, RiskClass
from mlops_agents.events.audit import SqliteAuditLog
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.sqlite import connect_checkpointer
from tests.agents.runtime.session_env import build_session_env
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


class RecordingObserver(TurnObserver):
    def __init__(self) -> None:
        self.started: list[tuple[str, str, str]] = []
        self.waiting: list[tuple[str, str, str]] = []
        self.ended: list[tuple[str, str, str, TurnEnd, str | None]] = []

    async def turn_started(self, project_id: str, agent: str, turn_id: str) -> None:
        self.started.append((project_id, agent, turn_id))

    async def turn_waiting(self, project_id: str, agent: str, turn_id: str) -> None:
        self.waiting.append((project_id, agent, turn_id))

    async def turn_ended(
        self,
        project_id: str,
        agent: str,
        turn_id: str,
        status: TurnEnd,
        final_text: str | None,
    ) -> None:
        self.ended.append((project_id, agent, turn_id, status, final_text))


class StoreAskGate(Gate):
    """A gate that records approvals into ApprovalStore and triggers LangGraph interrupt."""

    def __init__(self, databases: Databases) -> None:
        self.store = ApprovalStore(databases)
        self.decide_calls: list[GateCall] = []

    async def decide(self, calls: Sequence[GateCall]) -> dict[str, GateDecision]:
        self.decide_calls.extend(calls)
        results: dict[str, GateDecision] = {}
        for call in calls:
            preview = await call.tool.preview(call.ctx, call.args)
            record = await self.store.create(
                call.ctx.project_id,
                turn_id=call.ctx.turn_id or "trn_unknown",
                session_id=call.ctx.session_id,
                message_id=call.message_id,
                agent=call.ctx.agent,
                tool_call_id=call.tool_call_id,
                tool=call.tool.name,
                risk_class=call.tool.risk,
                preview=preview,
                status="pending",
            )
            results[call.tool_call_id] = GateDecision(
                outcome="ask",
                message=None,
                approval_id=record.id,
                preview=preview,
            )
        return results

    async def wait(
        self, project_id: str, turn_id: str, decisions: dict[str, GateDecision]
    ) -> dict[str, GateDecision]:
        interrupt(None)
        return decisions


async def collect_until_turn_waiting(
    sub: AsyncGenerator[StreamEvent, None],
    timeout: float = 10.0,  # noqa: ASYNC109
) -> list[StreamEvent]:
    events: list[StreamEvent] = []

    async def collector() -> None:
        async for ev in sub:
            events.append(ev)
            if isinstance(ev, TurnWaiting):
                break

    await asyncio.wait_for(collector(), timeout=timeout)
    return events


async def collect_until_turn_ended(
    sub: AsyncGenerator[StreamEvent, None],
    timeout: float = 10.0,  # noqa: ASYNC109
) -> list[StreamEvent]:
    events: list[StreamEvent] = []

    async def collector() -> None:
        async for ev in sub:
            events.append(ev)
            if isinstance(ev, TurnEnded):
                break

    await asyncio.wait_for(collector(), timeout=timeout)
    return events


async def _assert_thread_preserved(db_path: Path, thread_id: str) -> None:
    conn = await connect_checkpointer(db_path)
    saver = turn_checkpointer(conn)
    chk = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    assert chk is not None, f"Expected checkpointer thread {thread_id} to be preserved"
    await conn.close()


async def _assert_thread_deleted(db_path: Path, thread_id: str) -> None:
    conn = await connect_checkpointer(db_path)
    saver = turn_checkpointer(conn)
    chk = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    assert chk is None, f"Expected checkpointer thread {thread_id} to be deleted"
    await conn.close()


# ---------------------------------------------------------------------------
# Recovery Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovery_expires_pending_approvals_of_running_turn(
    databases: Databases, paths: DataPaths
) -> None:
    """At recovery, a turn left 'running' with a pending approval becomes 'interrupted',
    its approval becomes 'expired', the ApprovalPart in the message becomes 'expired',
    and the expiry is audited in AuditLog.
    """
    project_id = "prj_recexp"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    engine = await databases.project(project_id)

    audit = SqliteAuditLog(databases, secrets=[])
    store = SessionStore(databases, audit_log=audit)
    approval_store = ApprovalStore(databases)

    session_id = await store.session_id(project_id, "orchestrator")
    turn_id = await store.add_turn(project_id, session_id, "msg_user", "running")

    preview = Preview(kind="text", title="shell command", body="rm -rf /tmp/foo")
    apr_part = ApprovalPart(
        type="approval",
        approval_id="apr_test_1",
        tool_call_id="call_test_1",
        preview=preview,
        status="pending",
        note=None,
    )
    agent_msg = await store.add_message(
        project_id,
        session_id,
        author_kind="agent",
        author_name="orchestrator",
        parts=[TextPart(type="text", text="Executing action..."), apr_part],
        status="streaming",
        turn_id=turn_id,
    )

    await approval_store.create(
        project_id,
        turn_id=turn_id,
        session_id=session_id,
        message_id=agent_msg.id,
        agent="orchestrator",
        tool_call_id="call_test_1",
        tool="run_command",
        risk_class=RiskClass.RUN,
        preview=preview,
        status="pending",
        id="apr_test_1",
    )

    # Recover the project
    await store.recover(project_id)

    # 1. Turn is now interrupted
    t_status = await store.turn_status(project_id, turn_id)
    assert t_status == "interrupted"

    # 2. Approval in store is now expired
    apr_rec = await approval_store.get(project_id, "apr_test_1")
    assert apr_rec is not None
    assert apr_rec.status == "expired"
    assert apr_rec.decided_at is not None

    # 3. ApprovalPart in message is now expired
    updated_msg = await store.message(project_id, session_id, agent_msg.id)
    assert updated_msg is not None
    assert updated_msg.status == "interrupted"
    apr_parts = [p for p in updated_msg.parts if isinstance(p, ApprovalPart)]
    assert len(apr_parts) == 1
    assert apr_parts[0].status == "expired"

    # 4. Expiry was audited as decision="expired" with actor="system"
    async with engine.connect() as conn:
        from mlops_agents.storage.tables import audit as audit_table

        rows = (
            (
                await conn.execute(
                    select(audit_table).where(
                        audit_table.c.action == "approval",
                        audit_table.c.decision == "expired",
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    assert rows[0]["actor"] == "system"
    assert rows[0]["decision"] == "expired"
    assert rows[0]["target"] == "run_command"


@pytest.mark.asyncio
async def test_recovery_preserves_waiting_turn_and_pending_approval(
    databases: Databases, paths: DataPaths
) -> None:
    """A turn that is 'waiting' at recovery is preserved (neither turn nor approval is expired)."""
    project_id = "prj_recwait"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    audit = SqliteAuditLog(databases, secrets=[])
    store = SessionStore(databases, audit_log=audit)
    approval_store = ApprovalStore(databases)

    session_id = await store.session_id(project_id, "orchestrator")
    turn_id = await store.add_turn(project_id, session_id, "msg_user", "waiting")

    preview = Preview(kind="diff", title="edit foo.py", body="- old\n+ new")
    apr_part = ApprovalPart(
        type="approval",
        approval_id="apr_wait_1",
        tool_call_id="call_wait_1",
        preview=preview,
        status="pending",
        note=None,
    )
    agent_msg = await store.add_message(
        project_id,
        session_id,
        author_kind="agent",
        author_name="orchestrator",
        parts=[apr_part],
        status="waiting",
        turn_id=turn_id,
    )

    await approval_store.create(
        project_id,
        turn_id=turn_id,
        session_id=session_id,
        message_id=agent_msg.id,
        agent="orchestrator",
        tool_call_id="call_wait_1",
        tool="write_code",
        risk_class=RiskClass.WRITE_CODE,
        preview=preview,
        status="pending",
        id="apr_wait_1",
    )

    # Recover the project
    await store.recover(project_id)

    # 1. Turn is still waiting
    assert await store.turn_status(project_id, turn_id) == "waiting"

    # 2. Approval is still pending
    apr_rec = await approval_store.get(project_id, "apr_wait_1")
    assert apr_rec is not None
    assert apr_rec.status == "pending"
    assert apr_rec.decided_at is None

    # 3. Message and part are still waiting/pending
    msg = await store.message(project_id, session_id, agent_msg.id)
    assert msg is not None
    assert msg.status == "waiting"
    apr_parts = [p for p in msg.parts if isinstance(p, ApprovalPart)]
    assert len(apr_parts) == 1
    assert apr_parts[0].status == "pending"


# ---------------------------------------------------------------------------
# Stop on Waiting Turn Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_on_waiting_turn_cancels_approvals_and_ends_turn(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    """Stop on a waiting turn marks its pending approvals 'cancelled', updates ApprovalPart
    to 'cancelled', ends turn 'stopped', publishes turn_ended, notifies observers turn_ended,
    deletes its checkpoint thread, and marks message 'stopped'.
    """
    observer = RecordingObserver()
    gate = StoreAskGate(databases)
    audit = SqliteAuditLog(databases, secrets=[])

    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        observers=[observer],
        gate=gate,
        audit_log=audit,
    )
    manager = env.manager
    project_id = env.project_id
    store = env.store
    approval_store = ApprovalStore(databases)

    view = await manager.get(project_id, "orchestrator")
    session_id = view.id
    sub = manager.subscribe(project_id, "orchestrator", after_seq=view.last_seq)

    # 1. Send message triggering a tool call that asks for approval
    await manager.send(project_id, "orchestrator", "Run a command")
    events = await collect_until_turn_waiting(sub)
    turn_waiting_ev = next(e for e in events if isinstance(e, TurnWaiting))
    turn_id = turn_waiting_ev.data.turn_id

    # Verify initial waiting state
    assert await store.turn_status(project_id, turn_id) == "waiting"
    agent_msg = await store.turn_agent_message(project_id, session_id, turn_id)
    assert agent_msg is not None
    assert agent_msg.status == "waiting"
    pending_aprs = await approval_store.list_for_step(project_id, turn_id)
    assert len(pending_aprs) >= 1
    assert all(a.status == "pending" for a in pending_aprs)

    # Verify checkpoint thread exists
    db_path = store.db_path(project_id)
    await _assert_thread_preserved(db_path, turn_id)

    # 2. Stop the waiting turn
    await manager.stop(project_id, "orchestrator")

    # 3. Pending approvals are cancelled
    cancelled_aprs = await approval_store.list_for_step(project_id, turn_id)
    assert len(cancelled_aprs) >= 1
    for a in cancelled_aprs:
        assert a.status == "cancelled"
        assert a.decided_at is not None

    # 4. Message parts are cancelled, message is stopped
    updated_msg = await store.turn_agent_message(project_id, session_id, turn_id)
    assert updated_msg is not None
    assert updated_msg.status == "stopped"
    msg_aprs = [p for p in updated_msg.parts if isinstance(p, ApprovalPart)]
    assert len(msg_aprs) >= 1
    for p in msg_aprs:
        assert p.status == "cancelled"

    # 5. Turn is ended status="stopped"
    assert await store.turn_status(project_id, turn_id) == "stopped"

    # 6. Checkpoint thread is deleted
    await _assert_thread_deleted(db_path, turn_id)

    # 7. Observers heard turn_ended with "stopped"
    ended_calls = [c for c in observer.ended if c[2] == turn_id]
    assert len(ended_calls) == 1
    assert ended_calls[0][3] == "stopped"

    # 8. Audited as decision="cancelled" actor="user"
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        from mlops_agents.storage.tables import audit as audit_table

        rows = (
            (
                await conn.execute(
                    select(audit_table).where(
                        audit_table.c.action == "approval",
                        audit_table.c.decision == "cancelled",
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) >= 1
    assert rows[0]["actor"] == "user"
    assert rows[0]["decision"] == "cancelled"

    await manager.close()


@pytest.mark.asyncio
async def test_stop_on_waiting_turn_runs_next_queued_turn(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    """When a waiting turn is stopped, the next queued message for that session runs."""
    gate = StoreAskGate(databases)
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        gate=gate,
    )
    manager = env.manager
    project_id = env.project_id
    store = env.store

    view = await manager.get(project_id, "orchestrator")
    session_id = view.id
    sub = manager.subscribe(project_id, "orchestrator", after_seq=view.last_seq)

    # 1. Trigger first turn -> waiting
    await manager.send(project_id, "orchestrator", "First: tool call")
    events = await collect_until_turn_waiting(sub)
    first_turn_ev = next(e for e in events if isinstance(e, TurnWaiting))
    first_turn_id = first_turn_ev.data.turn_id

    # 2. Send second message while first is waiting -> queued
    await manager.send(project_id, "orchestrator", "Second: queued message")
    queued_turn = await store.next_queued_turn(project_id, session_id)
    assert queued_turn is not None
    second_turn_id, _ = queued_turn

    # 3. Stop the waiting turn
    await manager.stop(project_id, "orchestrator")

    # 4. Verify first turn is stopped
    assert await store.turn_status(project_id, first_turn_id) == "stopped"

    # 5. Verify the second turn is picked up and runs (it either completes, waits, or runs)
    # Collect events until second turn is either waiting or ended
    second_events = await collect_until_turn_waiting(sub)
    assert any(
        isinstance(e, (TurnWaiting, TurnEnded)) and e.data.turn_id == second_turn_id
        for e in second_events
    )

    # Second turn is no longer queued
    assert await store.turn_status(project_id, second_turn_id) in ("running", "waiting", "done")

    await manager.close()


@pytest.mark.asyncio
async def test_decision_racing_with_stop_stop_wins_decision_gets_409(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    """When Stop wins a race with a decision, Stop cancels the approval and stops the turn.
    The subsequent decision attempt gets 409 (approval_not_pending) and never resumes the turn.
    """
    from mlops_agents.agents.approvals.service import ApprovalsService
    from mlops_agents.errors import DomainError

    gate = StoreAskGate(databases)
    audit = SqliteAuditLog(databases, secrets=[])

    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        gate=gate,
        audit_log=audit,
    )
    manager = env.manager
    project_id = env.project_id
    store = env.store
    approval_store = ApprovalStore(databases)
    approvals_svc = ApprovalsService(databases=databases, sessions=manager, audit_log=audit)

    view = await manager.get(project_id, "orchestrator")
    sub = manager.subscribe(project_id, "orchestrator", after_seq=view.last_seq)

    # 1. Trigger turn -> waiting
    await manager.send(project_id, "orchestrator", "Run a command")
    events = await collect_until_turn_waiting(sub)
    turn_waiting_ev = next(e for e in events if isinstance(e, TurnWaiting))
    turn_id = turn_waiting_ev.data.turn_id

    pending_aprs = await approval_store.list_for_step(project_id, turn_id)
    assert len(pending_aprs) >= 1
    apr_id = pending_aprs[0].id

    # 2. Stop wins
    await manager.stop(project_id, "orchestrator")

    # 3. Decision attempt after Stop must get 409 (approval_not_pending)
    with pytest.raises(DomainError) as exc_info:
        await approvals_svc.decide(project_id, apr_id, approve=True)

    assert exc_info.value.code == "approval_not_pending"
    assert exc_info.value.status == 409

    # 4. Turn remains stopped and is never resumed
    assert await store.turn_status(project_id, turn_id) == "stopped"

    await manager.close()
