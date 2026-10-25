from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from mlops_agents.agents.approvals.contract import ApprovalView
from mlops_agents.agents.approvals.service import ApprovalsService
from mlops_agents.agents.approvals.settings import set_permission_level
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.contract import (
    ApprovalPart,
    MessageView,
    Part,
    TextPart,
    ToolCallPart,
)
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.tools.base import Preview, RiskClass
from mlops_agents.errors import DomainError
from mlops_agents.events.audit import SqliteAuditLog
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import sessions, turns


class FakeSessionsForApproval:
    """A test double for sessions with update_message and resume_turn tracking."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.resumed_turns: list[tuple[str, str, str]] = []

    async def update_message(
        self,
        project_id: str,
        session_id: str,
        message_id: str,
        transform: Callable[[list[Part]], list[Part]],
    ) -> MessageView:
        current = await self.store.message(project_id, session_id, message_id)
        if current is None:
            raise DomainError("message_not_found", "Message not found", status=404)
        new_parts = transform(list(current.parts))
        return await self.store.update_message(project_id, message_id, parts=new_parts)

    async def resume_turn(self, project_id: str, session_id: str, turn_id: str) -> None:
        self.resumed_turns.append((project_id, session_id, turn_id))


async def _setup_session_and_turn(
    databases: Databases,
    project_id: str,
    session_id: str = "ses_1",
    turn_id: str = "trn_1",
    agent: str = "orchestrator",
) -> None:
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            sessions.insert().values(
                id=session_id,
                agent=agent,
                summary=None,
                compacted_through=None,
                created_at="2026-10-25T10:00:00Z",
            )
        )
        await conn.execute(
            turns.insert().values(
                id=turn_id,
                session_id=session_id,
                trigger_message_id="msg_trigger",
                status="waiting",
                error=None,
                created_at="2026-10-25T10:00:00Z",
                started_at="2026-10-25T10:00:00Z",
                ended_at=None,
            )
        )


@pytest.mark.asyncio
async def test_approvals_service_pending(databases: Databases, tmp_path: Path) -> None:
    project_id = "prj_pending"
    await _setup_session_and_turn(databases, project_id)
    store = ApprovalStore(databases)
    preview = Preview(kind="diff", title="edit main.py", body="- old\n+ new")

    apr1 = await store.create(
        project_id,
        turn_id="trn_1",
        session_id="ses_1",
        message_id="msg_1",
        agent="orchestrator",
        tool_call_id="call_1",
        tool="edit_file",
        risk_class=RiskClass.WRITE_CODE,
        preview=preview,
    )
    apr2 = await store.create(
        project_id,
        turn_id="trn_1",
        session_id="ses_1",
        message_id="msg_1",
        agent="orchestrator",
        tool_call_id="call_2",
        tool="bash",
        risk_class=RiskClass.RUN,
        preview=preview,
    )
    # apr3 already decided
    await store.create(
        project_id,
        turn_id="trn_1",
        session_id="ses_1",
        message_id="msg_1",
        agent="orchestrator",
        tool_call_id="call_3",
        tool="bash",
        risk_class=RiskClass.RUN,
        preview=preview,
        status="approved",
    )

    fake_sessions = FakeSessionsForApproval(SessionStore(databases))
    service = ApprovalsService(databases=databases, sessions=fake_sessions)

    pending = await service.pending(project_id)
    assert len(pending) == 2
    assert [p.id for p in pending] == [apr1.id, apr2.id]
    assert all(isinstance(p, ApprovalView) for p in pending)


@pytest.mark.asyncio
async def test_approvals_service_decide_approve(databases: Databases, tmp_path: Path) -> None:
    project_id = "prj_decideappr"
    await _setup_session_and_turn(databases, project_id)
    await set_permission_level(databases, project_id, "ask")

    session_store = SessionStore(databases)
    apr_part = ApprovalPart(
        type="approval",
        approval_id="apr_test1",
        tool_call_id="call_1",
        preview=Preview(kind="diff", title="Edit", body="diff"),
        status="pending",
        note=None,
    )
    agent_msg = await session_store.add_message(
        project_id,
        "ses_1",
        turn_id="trn_1",
        author_kind="agent",
        author_name="orchestrator",
        parts=[
            TextPart(type="text", text="Editing file..."),
            ToolCallPart(type="tool_call", id="call_1", name="edit_file", input={"file": "foo"}),
            apr_part,
        ],
        status="waiting",
    )

    store = ApprovalStore(databases)
    apr = await store.create(
        project_id,
        id="apr_test1",
        turn_id="trn_1",
        session_id="ses_1",
        message_id=agent_msg.id,
        agent="orchestrator",
        tool_call_id="call_1",
        tool="edit_file",
        risk_class=RiskClass.WRITE_CODE,
        preview=Preview(kind="diff", title="Edit", body="diff"),
    )

    audit_log = SqliteAuditLog(databases, secrets=[])
    event_log = SqliteEventLog(databases)
    fake_sessions = FakeSessionsForApproval(session_store)

    service = ApprovalsService(
        databases=databases,
        sessions=fake_sessions,
        audit_log=audit_log,
        event_log=event_log,
    )

    view = await service.decide(project_id, apr.id, approve=True, note="Looks great")
    assert view.id == apr.id
    assert view.status == "approved"
    assert view.note == "Looks great"
    assert view.decided_at is not None

    # Check session message part updated
    updated_msg = await session_store.message(project_id, "ses_1", agent_msg.id)
    assert updated_msg is not None
    updated_apr_part = next(p for p in updated_msg.parts if isinstance(p, ApprovalPart))
    assert updated_apr_part.status == "approved"
    assert updated_apr_part.note == "Looks great"

    # Check audit log recorded
    from mlops_agents.storage.tables import audit

    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        audit_rows = (await conn.execute(audit.select())).mappings().all()
    assert len(audit_rows) == 1
    assert audit_rows[0]["actor"] == "user"
    assert audit_rows[0]["action"] == "approval"
    assert audit_rows[0]["decision"] == "approved"
    assert audit_rows[0]["risk_class"] == RiskClass.WRITE_CODE.value
    assert audit_rows[0]["permission_level"] == "ask"

    # Check event log recorded
    events = await event_log.history(project_id, types=["approval.decided"])
    assert len(events) == 1
    assert events[0].source == "user"
    assert events[0].subject_id == apr.id
    assert events[0].payload["approve"] is True
    assert events[0].payload["note"] == "Looks great"

    # Check turn resumed
    assert fake_sessions.resumed_turns == [(project_id, "ses_1", "trn_1")]


@pytest.mark.asyncio
async def test_approvals_service_decide_reject(databases: Databases, tmp_path: Path) -> None:
    project_id = "prj_deciderej"
    await _setup_session_and_turn(databases, project_id)
    await set_permission_level(databases, project_id, "ask")

    session_store = SessionStore(databases)
    apr_part = ApprovalPart(
        type="approval",
        approval_id="apr_test2",
        tool_call_id="call_2",
        preview=Preview(kind="command", title="Bash", body="rm -rf /"),
        status="pending",
        note=None,
    )
    agent_msg = await session_store.add_message(
        project_id,
        "ses_1",
        turn_id="trn_1",
        author_kind="agent",
        author_name="orchestrator",
        parts=[apr_part],
        status="waiting",
    )

    store = ApprovalStore(databases)
    apr = await store.create(
        project_id,
        id="apr_test2",
        turn_id="trn_1",
        session_id="ses_1",
        message_id=agent_msg.id,
        agent="orchestrator",
        tool_call_id="call_2",
        tool="bash",
        risk_class=RiskClass.RUN,
        preview=Preview(kind="command", title="Bash", body="rm -rf /"),
    )

    audit_log = SqliteAuditLog(databases, secrets=[])
    event_log = SqliteEventLog(databases)
    fake_sessions = FakeSessionsForApproval(session_store)

    service = ApprovalsService(
        databases=databases,
        sessions=fake_sessions,
        audit_log=audit_log,
        event_log=event_log,
    )

    view = await service.decide(project_id, apr.id, approve=False, note="Dangerous command")
    assert view.id == apr.id
    assert view.status == "rejected"
    assert view.note == "Dangerous command"

    from mlops_agents.storage.tables import audit

    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        audit_rows = (await conn.execute(audit.select())).mappings().all()
    assert len(audit_rows) == 1
    assert audit_rows[0]["decision"] == "rejected"

    events = await event_log.history(project_id, types=["approval.decided"])
    assert len(events) == 1
    assert events[0].payload["approve"] is False
    assert events[0].payload["note"] == "Dangerous command"

    assert fake_sessions.resumed_turns == [(project_id, "ses_1", "trn_1")]


@pytest.mark.asyncio
async def test_approvals_service_errors(databases: Databases, tmp_path: Path) -> None:
    project_id = "prj_decideerr"
    await _setup_session_and_turn(databases, project_id)

    store = ApprovalStore(databases)
    apr = await store.create(
        project_id,
        id="apr_test3",
        turn_id="trn_1",
        session_id="ses_1",
        message_id="msg_1",
        agent="orchestrator",
        tool_call_id="call_3",
        tool="bash",
        risk_class=RiskClass.RUN,
        preview=Preview(kind="command", title="Bash", body="ls"),
        status="approved",
    )

    fake_sessions = FakeSessionsForApproval(SessionStore(databases))
    service = ApprovalsService(databases=databases, sessions=fake_sessions)

    # Deciding non-pending raises 409
    with pytest.raises(DomainError) as exc_info:
        await service.decide(project_id, apr.id, approve=True, note=None)
    assert exc_info.value.status == 409
    assert exc_info.value.code == "approval_not_pending"

    # Deciding nonexistent raises 404
    with pytest.raises(DomainError) as exc_info:
        await service.decide(project_id, "nonexistent_apr", approve=True, note=None)
    assert exc_info.value.status == 404
    assert exc_info.value.code == "approval_not_found"


@pytest.mark.asyncio
async def test_approvals_service_multiple_in_step(databases: Databases, tmp_path: Path) -> None:
    project_id = "prj_multidecide"
    await _setup_session_and_turn(databases, project_id)

    store = ApprovalStore(databases)
    session_store = SessionStore(databases)
    fake_sessions = FakeSessionsForApproval(session_store)

    preview = Preview(kind="diff", title="Edit", body="diff")
    agent_msg = await session_store.add_message(
        project_id,
        "ses_1",
        turn_id="trn_1",
        author_kind="agent",
        author_name="orchestrator",
        parts=[
            ApprovalPart(
                type="approval",
                approval_id="apr_m1",
                tool_call_id="call_m1",
                preview=preview,
                status="pending",
                note=None,
            ),
            ApprovalPart(
                type="approval",
                approval_id="apr_m2",
                tool_call_id="call_m2",
                preview=preview,
                status="pending",
                note=None,
            ),
        ],
        status="waiting",
    )

    apr1 = await store.create(
        project_id,
        id="apr_m1",
        turn_id="trn_1",
        session_id="ses_1",
        message_id=agent_msg.id,
        agent="orchestrator",
        tool_call_id="call_m1",
        tool="edit_file",
        risk_class=RiskClass.WRITE_CODE,
        preview=preview,
    )
    apr2 = await store.create(
        project_id,
        id="apr_m2",
        turn_id="trn_1",
        session_id="ses_1",
        message_id=agent_msg.id,
        agent="orchestrator",
        tool_call_id="call_m2",
        tool="bash",
        risk_class=RiskClass.RUN,
        preview=preview,
    )

    service = ApprovalsService(databases=databases, sessions=fake_sessions)

    # Decide first approval: turn should NOT resume yet
    await service.decide(project_id, apr1.id, approve=True, note="First OK")
    assert fake_sessions.resumed_turns == []

    # Decide second approval: turn should resume now
    await service.decide(project_id, apr2.id, approve=True, note="Second OK")
    assert fake_sessions.resumed_turns == [(project_id, "ses_1", "trn_1")]


@pytest.mark.asyncio
async def test_approvals_service_concurrent_decisions(databases: Databases, tmp_path: Path) -> None:
    project_id = "prj_concurrentdecide"
    await _setup_session_and_turn(databases, project_id)

    store = ApprovalStore(databases)
    session_store = SessionStore(databases)
    fake_sessions = FakeSessionsForApproval(session_store)

    preview = Preview(kind="diff", title="Edit", body="diff")
    agent_msg = await session_store.add_message(
        project_id,
        "ses_1",
        turn_id="trn_1",
        author_kind="agent",
        author_name="orchestrator",
        parts=[
            ApprovalPart(
                type="approval",
                approval_id="apr_c1",
                tool_call_id="call_c1",
                preview=preview,
                status="pending",
                note=None,
            ),
            ApprovalPart(
                type="approval",
                approval_id="apr_c2",
                tool_call_id="call_c2",
                preview=preview,
                status="pending",
                note=None,
            ),
        ],
        status="waiting",
    )

    apr1 = await store.create(
        project_id,
        id="apr_c1",
        turn_id="trn_1",
        session_id="ses_1",
        message_id=agent_msg.id,
        agent="orchestrator",
        tool_call_id="call_c1",
        tool="edit_file",
        risk_class=RiskClass.WRITE_CODE,
        preview=preview,
    )
    apr2 = await store.create(
        project_id,
        id="apr_c2",
        turn_id="trn_1",
        session_id="ses_1",
        message_id=agent_msg.id,
        agent="orchestrator",
        tool_call_id="call_c2",
        tool="bash",
        risk_class=RiskClass.RUN,
        preview=preview,
    )

    service = ApprovalsService(databases=databases, sessions=fake_sessions)

    # Decide both concurrently
    results = await asyncio.gather(
        service.decide(project_id, apr1.id, approve=True, note="First"),
        service.decide(project_id, apr2.id, approve=True, note="Second"),
    )
    assert len(results) == 2
    assert results[0].status == "approved"
    assert results[1].status == "approved"

    # Both parts updated
    msg = await session_store.message(project_id, "ses_1", agent_msg.id)
    assert msg is not None
    apr_parts = [p for p in msg.parts if isinstance(p, ApprovalPart)]
    assert len(apr_parts) == 2
    assert apr_parts[0].status == "approved"
    assert apr_parts[1].status == "approved"

    # Turn resume was called at least once
    assert len(fake_sessions.resumed_turns) >= 1
