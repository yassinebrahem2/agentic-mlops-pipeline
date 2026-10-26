"""Tests for ApprovalStore."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import insert, select

from mlops_agents.agents.approvals.store import ApprovalRecord, ApprovalStore
from mlops_agents.agents.tools.base import Preview, RiskClass
from mlops_agents.clock import now_iso
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import approvals, sessions, turns


async def _setup_session_and_turn(
    databases: Databases, project_id: str, session_id: str = "ses_1", turn_id: str = "trn_1"
) -> None:
    engine = await databases.project(project_id)
    now = now_iso()
    async with engine.begin() as conn:
        await conn.execute(
            insert(sessions).values(
                id=session_id,
                agent="orchestrator",
                summary=None,
                compacted_through=None,
                created_at=now,
            )
        )
        await conn.execute(
            insert(turns).values(
                id=turn_id,
                session_id=session_id,
                trigger_message_id="msg_1",
                status="running",
                error=None,
                created_at=now,
                started_at=now,
                ended_at=None,
            )
        )


@pytest.mark.asyncio
async def test_approval_store_create(databases: Databases, paths: DataPaths) -> None:
    project_id = "prj_store1"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await _setup_session_and_turn(databases, project_id, "ses_1", "trn_1")

    store = ApprovalStore(databases)
    preview = Preview(
        kind="diff", title="edit foo.py", body="--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new"
    )

    record = await store.create(
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

    assert isinstance(record, ApprovalRecord)
    assert record.id.startswith("apr_")
    assert record.turn_id == "trn_1"
    assert record.session_id == "ses_1"
    assert record.message_id == "msg_1"
    assert record.agent == "orchestrator"
    assert record.tool_call_id == "call_1"
    assert record.tool == "edit_file"
    assert record.risk_class == RiskClass.WRITE_CODE
    assert record.preview == preview
    assert record.status == "pending"
    assert record.note is None
    assert record.decided_at is None
    assert record.created_at is not None

    # Verify directly in SQLite approvals table
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        row = (
            (await conn.execute(select(approvals).where(approvals.c.id == record.id)))
            .mappings()
            .one()
        )
        assert row["turn_id"] == "trn_1"
        assert row["tool_call_id"] == "call_1"
        assert row["status"] == "pending"
        preview_data = json.loads(row["preview"])
        assert preview_data["kind"] == "diff"
        assert preview_data["body"] == preview.body


@pytest.mark.asyncio
async def test_approval_store_get(databases: Databases, paths: DataPaths) -> None:
    project_id = "prj_store2"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await _setup_session_and_turn(databases, project_id, "ses_1", "trn_1")

    store = ApprovalStore(databases)
    preview = Preview(kind="command", title="run bash", body="pytest")
    created = await store.create(
        project_id,
        turn_id="trn_1",
        session_id="ses_1",
        message_id="msg_1",
        agent="orchestrator",
        tool_call_id="call_get",
        tool="bash",
        risk_class=RiskClass.RUN,
        preview=preview,
    )

    fetched = await store.get(project_id, created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.tool_call_id == "call_get"
    assert fetched.preview == preview

    # Non-existent approval
    missing = await store.get(project_id, "apr_nonexistent")
    assert missing is None


@pytest.mark.asyncio
async def test_approval_store_same_tool_call_id_creates_separate_approvals(
    databases: Databases, paths: DataPaths
) -> None:
    project_id = "prj_store4"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await _setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            insert(turns).values(
                id="trn_2",
                session_id="ses_1",
                trigger_message_id="msg_2",
                status="running",
                error=None,
                created_at=now_iso(),
                started_at=now_iso(),
                ended_at=None,
            )
        )

    store = ApprovalStore(databases)
    preview = Preview(kind="command", title="run", body="ls")

    first = await store.create(
        project_id,
        turn_id="trn_1",
        session_id="ses_1",
        message_id="msg_1",
        agent="orchestrator",
        tool_call_id="call_same_id",
        tool="bash",
        risk_class=RiskClass.RUN,
        preview=preview,
    )

    second = await store.create(
        project_id,
        turn_id="trn_2",
        session_id="ses_1",
        message_id="msg_2",
        agent="orchestrator",
        tool_call_id="call_same_id",
        tool="bash",
        risk_class=RiskClass.RUN,
        preview=preview,
    )

    assert second.id != first.id
    assert second.tool_call_id == first.tool_call_id == "call_same_id"

    # Both approvals exist in the database
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(select(approvals).where(approvals.c.tool_call_id == "call_same_id"))
        ).all()
        assert len(rows) == 2


@pytest.mark.asyncio
async def test_approval_store_update_status_and_decide(
    databases: Databases, paths: DataPaths
) -> None:
    project_id = "prj_store5"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await _setup_session_and_turn(databases, project_id, "ses_1", "trn_1")

    store = ApprovalStore(databases)
    preview = Preview(kind="text", title="action", body="body")

    rec1 = await store.create(
        project_id,
        turn_id="trn_1",
        session_id="ses_1",
        message_id="msg_1",
        agent="orchestrator",
        tool_call_id="call_u1",
        tool="test_tool",
        risk_class=RiskClass.RUN,
        preview=preview,
    )

    updated = await store.update_status(project_id, rec1.id, "approved")
    assert updated is not None
    assert updated.status == "approved"
    assert updated.decided_at is not None

    rec2 = await store.create(
        project_id,
        turn_id="trn_1",
        session_id="ses_1",
        message_id="msg_1",
        agent="orchestrator",
        tool_call_id="call_u2",
        tool="test_tool",
        risk_class=RiskClass.RUN,
        preview=preview,
    )

    decided = await store.decide(project_id, rec2.id, approve=False, note="rejected by user")
    assert decided.status == "rejected"
    assert decided.note == "rejected by user"
    assert decided.decided_at is not None


@pytest.mark.asyncio
async def test_approval_store_list_for_step(databases: Databases, paths: DataPaths) -> None:
    project_id = "prj_store6"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await _setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            insert(turns).values(
                id="trn_2",
                session_id="ses_1",
                trigger_message_id="msg_2",
                status="running",
                error=None,
                created_at=now_iso(),
                started_at=now_iso(),
                ended_at=None,
            )
        )

    store = ApprovalStore(databases)
    preview = Preview(kind="text", title="action", body="body")

    apr1 = await store.create(
        project_id,
        turn_id="trn_1",
        session_id="ses_1",
        message_id="msg_1",
        agent="orchestrator",
        tool_call_id="call_step_1",
        tool="tool1",
        risk_class=RiskClass.WRITE_CODE,
        preview=preview,
    )
    apr2 = await store.create(
        project_id,
        turn_id="trn_1",
        session_id="ses_1",
        message_id="msg_1",
        agent="orchestrator",
        tool_call_id="call_step_2",
        tool="tool2",
        risk_class=RiskClass.RUN,
        preview=preview,
    )
    apr3 = await store.create(
        project_id,
        turn_id="trn_2",
        session_id="ses_1",
        message_id="msg_2",
        agent="orchestrator",
        tool_call_id="call_step_3",
        tool="tool3",
        risk_class=RiskClass.DEPLOY,
        preview=preview,
    )

    turn1_approvals = await store.list_for_step(project_id, "trn_1")
    assert [a.id for a in turn1_approvals] == [apr1.id, apr2.id]

    by_tool_call_ids = await store.list_for_step(project_id, "trn_1", tool_call_ids=["call_step_2"])
    assert [a.id for a in by_tool_call_ids] == [apr2.id]

    by_approval_ids = await store.list_for_step(project_id, approval_ids=[apr1.id, apr3.id])
    assert [a.id for a in by_approval_ids] == [apr1.id, apr3.id]
