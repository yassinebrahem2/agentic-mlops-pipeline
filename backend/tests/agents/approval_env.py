"""A project with one session, one waiting turn and its agent message, for the approval tests;
an already-decided approval; and an observer that records what it is told."""

from __future__ import annotations

import asyncio

from sqlalchemy import update

from mlops_agents.agents.approvals.contract import ApprovalView
from mlops_agents.agents.approvals.store import ApprovalRecord, ApprovalStore, DecidedBy
from mlops_agents.agents.runtime.contract import DecidedApprovalStatus, Part
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.tools.base import Preview, RiskClass
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import approvals, sessions, turns

STALL_PREVIEW = Preview(kind="text", title="Stalled: no usable key", body="key 1: limited")
TOOL_PREVIEW = Preview(kind="diff", title="edit main.py", body="- old\n+ new")


async def add_turn(
    databases: Databases,
    project_id: str,
    *,
    session_id: str = "ses_1",
    turn_id: str = "trn_1",
    agent: str = "orchestrator",
    status: str = "waiting",
    parts: list[Part] | None = None,
) -> str:
    """The session (made once), a turn of it and the turn's agent message; returns the message ID."""
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        found = (await conn.execute(sessions.select().where(sessions.c.id == session_id))).first()
        if found is None:
            await conn.execute(
                sessions.insert().values(
                    id=session_id,
                    agent=agent,
                    summary=None,
                    compacted_through=None,
                    created_at="2026-10-28T10:00:00Z",
                )
            )
        await conn.execute(
            turns.insert().values(
                id=turn_id,
                session_id=session_id,
                trigger_message_id="msg_trigger",
                status=status,
                error=None,
                created_at="2026-10-28T10:00:00Z",
                started_at="2026-10-28T10:00:00Z",
                ended_at=None,
            )
        )
    message = await SessionStore(databases).add_message(
        project_id,
        session_id,
        turn_id=turn_id,
        author_kind="agent",
        author_name=agent,
        parts=parts or [],
        status=status if status == "waiting" else "complete",
    )
    return message.id


async def seed_decided_approval(
    databases: Databases,
    store: ApprovalStore,
    project_id: str,
    *,
    status: DecidedApprovalStatus,
    id: str | None = None,
    turn_id: str = "trn_1",
    session_id: str = "ses_1",
    message_id: str = "msg_1",
    tool_call_id: str = "call_1",
    tool: str = "edit_file",
    risk_class: RiskClass = RiskClass.WRITE_CODE,
    preview: Preview = TOOL_PREVIEW,
    decided_by: DecidedBy = "user",
) -> ApprovalRecord:
    """A tool approval decided and settled before the test starts. Production never inserts a
    decided row, so it is settled here: nothing later re-tells or re-settles it."""
    record = await store.create(
        project_id,
        id=id,
        turn_id=turn_id,
        session_id=session_id,
        message_id=message_id,
        agent="orchestrator",
        tool_call_id=tool_call_id,
        tool=tool,
        risk_class=risk_class,
        preview=preview,
        status=status,
    )
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            update(approvals)
            .where(approvals.c.id == record.id)
            .values(settled_at=record.decided_at, decided_by=decided_by)
        )
    settled = await store.get(project_id, record.id)
    assert settled is not None
    return settled


class RecordingObserver:
    """Records each call as (event, approval ID, status); `delay` makes every call slow."""

    def __init__(self, calls: list[tuple[str, str, str]] | None = None, delay: float = 0) -> None:
        self.calls: list[tuple[str, str, str]] = calls if calls is not None else []
        self.views: list[ApprovalView] = []
        self._delay = delay

    async def asked(self, view: ApprovalView) -> None:
        await asyncio.sleep(self._delay)
        self.calls.append(("asked", view.id, view.status))
        self.views.append(view)

    async def decided(self, view: ApprovalView) -> None:
        await asyncio.sleep(self._delay)
        self.calls.append(("decided", view.id, view.status))
        self.views.append(view)


class RaisingObserver:
    """Fails every call, with a message that must not reach the log unscrubbed."""

    def __init__(self) -> None:
        self.calls = 0

    async def asked(self, view: ApprovalView) -> None:
        self.calls += 1
        raise RuntimeError("observer failed at https://user:hunter2@example.com/x")

    async def decided(self, view: ApprovalView) -> None:
        self.calls += 1
        raise RuntimeError("observer failed at https://user:hunter2@example.com/x")
