"""Tests for Task 2: A failed turn leaves no pending proposal behind.

When a turn ends (any status) with drafts it created that never got an approval,
mark them rejected and record a proposal.decided event.
Proposals with an approval are untouched by this draft cleanup.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock

import httpx2
import pytest
from pydantic import BaseModel

from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.contract import TextPart, TurnEnded
from mlops_agents.agents.runtime.executor import TurnExecutor
from mlops_agents.agents.runtime.hub import AsyncRLock, SessionHub
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.runtime.turn_end import TurnEnds
from mlops_agents.agents.runtime.waiting import _Ending
from mlops_agents.agents.tools.base import (
    AgentTool,
    Preview,
    RiskClass,
    ToolContext,
    ToolOutput,
)
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.store import ProposalStore
from mlops_agents.storage.databases import Databases
from tests.agents.gate_helpers import DummyArgs
from tests.agents.runtime.session_env import build_session_env, collect_until_turn_ended
from tests.agents.runtime.test_resuming_turns import make_tool_reg
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


@pytest.mark.asyncio
async def test_proposal_store_list_pending_without_approval_for_turn(databases: Databases) -> None:
    store = ProposalStore(databases)
    project_id = "prj_turndrafts"

    # 1. Draft for trn_1, pending, no approval -> should be listed
    draft1, _ = await store.create_draft(
        project_id,
        proposal_id="prp_1",
        agent="architect",
        kind="connections",
        key="key_1",
        payload_json="{}",
        turn_id="trn_1",
        tool_call_id="call_1",
    )

    # 2. Draft for trn_1, pending, WITH approval -> should NOT be listed
    draft2, _ = await store.create_draft(
        project_id,
        proposal_id="prp_2",
        agent="architect",
        kind="connections",
        key="key_2",
        payload_json="{}",
        turn_id="trn_1",
        tool_call_id="call_2",
    )
    await store.attach_and_supersede(project_id, draft2.id, "apr_2")

    # 3. Draft for trn_1, rejected, no approval -> should NOT be listed
    draft3, _ = await store.create_draft(
        project_id,
        proposal_id="prp_3",
        agent="architect",
        kind="connections",
        key="key_3",
        payload_json="{}",
        turn_id="trn_1",
        tool_call_id="call_3",
    )
    await store.update_status(project_id, draft3.id, status="rejected")

    # 4. Draft for trn_2, pending, no approval -> should NOT be listed for trn_1
    draft4, _ = await store.create_draft(
        project_id,
        proposal_id="prp_4",
        agent="architect",
        kind="connections",
        key="key_4",
        payload_json="{}",
        turn_id="trn_2",
        tool_call_id="call_4",
    )

    # Query for trn_1
    found = await store.list_pending_without_approval_for_turn(project_id, "trn_1")
    assert len(found) == 1
    assert found[0].id == draft1.id

    # Query for trn_2
    found_2 = await store.list_pending_without_approval_for_turn(project_id, "trn_2")
    assert len(found_2) == 1
    assert found_2[0].id == draft4.id

    # Query for trn_3 (non-existent)
    found_3 = await store.list_pending_without_approval_for_turn(project_id, "trn_3")
    assert found_3 == []


@pytest.mark.asyncio
@pytest.mark.parametrize("end_status", ["failed", "stopped", "done"])
async def test_turn_end_rejects_pending_draft_without_approval(
    databases: Databases, end_status: str
) -> None:
    session_store = SessionStore(databases)
    proposal_store = ProposalStore(databases)
    approval_store = ApprovalStore(databases)
    event_log = SqliteEventLog(databases)

    project_id = f"prj_turn{end_status}"
    session_id = await session_store.session_id(project_id, "architect")
    msg = await session_store.add_message(
        project_id,
        session_id,
        author_kind="user",
        author_name="User",
        parts=[TextPart(type="text", text="run")],
        status="complete",
    )
    turn_id = await session_store.add_turn(project_id, session_id, msg.id, "running")

    # Create a proposal draft during this turn
    draft, _ = await proposal_store.create_draft(
        project_id,
        proposal_id=f"prp_{end_status}",
        agent="architect",
        kind="connections",
        key=f"key_{end_status}",
        payload_json="{}",
        turn_id=turn_id,
        tool_call_id="call_1",
    )
    assert draft.status == "pending"
    assert draft.approval_id is None

    turn_ends = TurnEnds(
        store=session_store,
        executor=MagicMock(spec=TurnExecutor),
        observers=(),
        approval_store=approval_store,
        audit_log=None,
        event_log=event_log,
        proposal_store=proposal_store,
    )

    hub = SessionHub(session_id)
    lock = AsyncRLock()
    ending = _Ending(
        status=end_status,  # type: ignore[arg-type]
        error="Turn failed" if end_status == "failed" else None,
        stopped_reason="User stop" if end_status == "stopped" else None,
    )

    await turn_ends.end(
        project_id,
        session_id,
        "architect",
        turn_id,
        msg.id,
        hub,
        lock,
        ending,
    )

    # The draft should now be rejected
    record = await proposal_store.get(project_id, draft.id)
    assert record is not None
    assert record.status == "rejected"
    assert record.decided_at is not None

    # Check event log recorded proposal.decided
    events = await event_log.history(project_id, types=["proposal.decided"])
    assert len(events) == 1
    assert events[0].type == "proposal.decided"
    assert events[0].source == "system"
    assert events[0].subject_id == draft.id
    assert events[0].payload == {
        "proposal_id": draft.id,
        "kind": draft.kind,
        "status": "rejected",
    }


@pytest.mark.asyncio
async def test_turn_end_leaves_proposal_with_approval_untouched(databases: Databases) -> None:
    session_store = SessionStore(databases)
    proposal_store = ProposalStore(databases)
    approval_store = ApprovalStore(databases)
    event_log = SqliteEventLog(databases)

    project_id = "prj_turnwithapr"
    session_id = await session_store.session_id(project_id, "architect")
    msg = await session_store.add_message(
        project_id,
        session_id,
        author_kind="user",
        author_name="User",
        parts=[TextPart(type="text", text="run")],
        status="complete",
    )
    turn_id = await session_store.add_turn(project_id, session_id, msg.id, "running")

    # Create a proposal draft during this turn and attach an approval
    draft, _ = await proposal_store.create_draft(
        project_id,
        proposal_id="prp_with_apr",
        agent="architect",
        kind="connections",
        key="key_with_apr",
        payload_json="{}",
        turn_id=turn_id,
        tool_call_id="call_1",
    )
    await proposal_store.attach_and_supersede(project_id, draft.id, "apr_123")

    turn_ends = TurnEnds(
        store=session_store,
        executor=MagicMock(spec=TurnExecutor),
        observers=(),
        approval_store=approval_store,
        audit_log=None,
        event_log=event_log,
        proposal_store=proposal_store,
    )

    hub = SessionHub(session_id)
    lock = AsyncRLock()
    ending = _Ending(status="failed", error="Turn failed")

    await turn_ends.end(
        project_id,
        session_id,
        "architect",
        turn_id,
        msg.id,
        hub,
        lock,
        ending,
    )

    # The draft should NOT have been rejected by the proposal draft cleanup
    record = await proposal_store.get(project_id, draft.id)
    assert record is not None
    assert record.status == "pending"
    assert record.approval_id == "apr_123"

    # No proposal.decided event should have been emitted
    events = await event_log.history(project_id, types=["proposal.decided"])
    assert len(events) == 0


@pytest.mark.asyncio
async def test_turn_end_default_proposal_store(databases: Databases) -> None:
    session_store = SessionStore(databases)
    approval_store = ApprovalStore(databases)
    proposal_store = ProposalStore(databases)

    project_id = "prj_turndefaultstore"
    session_id = await session_store.session_id(project_id, "architect")
    msg = await session_store.add_message(
        project_id,
        session_id,
        author_kind="user",
        author_name="User",
        parts=[TextPart(type="text", text="run")],
        status="complete",
    )
    turn_id = await session_store.add_turn(project_id, session_id, msg.id, "running")

    draft, _ = await proposal_store.create_draft(
        project_id,
        proposal_id="prp_default",
        agent="architect",
        kind="connections",
        key="key_default",
        payload_json="{}",
        turn_id=turn_id,
        tool_call_id="call_1",
    )

    # Initialize TurnEnds without proposal_store argument (uses default)
    turn_ends = TurnEnds(
        store=session_store,
        executor=MagicMock(spec=TurnExecutor),
        observers=(),
        approval_store=approval_store,
        audit_log=None,
        event_log=None,
    )

    hub = SessionHub(session_id)
    lock = AsyncRLock()
    ending = _Ending(status="failed", error="Turn failed")

    await turn_ends.end(
        project_id,
        session_id,
        "architect",
        turn_id,
        msg.id,
        hub,
        lock,
        ending,
    )

    record = await proposal_store.get(project_id, draft.id)
    assert record is not None
    assert record.status == "rejected"


@pytest.mark.asyncio
async def test_turn_end_error_resilience(databases: Databases) -> None:
    session_store = SessionStore(databases)
    approval_store = ApprovalStore(databases)
    failing_proposal_store = MagicMock(spec=ProposalStore)
    failing_proposal_store.list_pending_without_approval_for_turn.side_effect = RuntimeError(
        "DB locked"
    )

    project_id = "prj_turnerror"
    session_id = await session_store.session_id(project_id, "architect")
    msg = await session_store.add_message(
        project_id,
        session_id,
        author_kind="user",
        author_name="User",
        parts=[TextPart(type="text", text="run")],
        status="complete",
    )
    turn_id = await session_store.add_turn(project_id, session_id, msg.id, "running")

    turn_ends = TurnEnds(
        store=session_store,
        executor=MagicMock(spec=TurnExecutor),
        observers=(),
        approval_store=approval_store,
        audit_log=None,
        event_log=None,
        proposal_store=failing_proposal_store,
    )

    hub = SessionHub(session_id)
    lock = AsyncRLock()
    ending = _Ending(status="failed", error="Turn failed")

    # Should not raise despite proposal_store failing
    await turn_ends.end(
        project_id,
        session_id,
        "architect",
        turn_id,
        msg.id,
        hub,
        lock,
        ending,
    )


class _DraftCreatingTool(AgentTool[DummyArgs]):
    name: ClassVar[str] = "create_proposal_tool"
    description: ClassVar[str] = "Creates a proposal draft during preview"
    risk: ClassVar[RiskClass] = RiskClass.PROPOSE
    args_model: ClassVar[type[BaseModel]] = DummyArgs

    def __init__(self, proposal_store: ProposalStore) -> None:
        self._store = proposal_store
        self.created_id: str | None = None

    async def preview(self, ctx: ToolContext, args: DummyArgs) -> Preview:
        rec, _ = await self._store.create_draft(
            ctx.project_id,
            proposal_id="prp_preview1",
            agent=ctx.agent,
            kind="connections",
            key="conn_key",
            payload_json="{}",
            turn_id=ctx.turn_id,
            tool_call_id=ctx.tool_call_id,
        )
        self.created_id = rec.id
        return Preview(kind="text", title="Proposal", body="draft")

    async def run(self, ctx: ToolContext, args: DummyArgs) -> ToolOutput:
        return ToolOutput("ok")


@pytest.mark.asyncio
async def test_failed_turn_leaves_preview_draft_rejected_integration(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal_store = ProposalStore(databases)
    event_log = SqliteEventLog(databases)
    tool = _DraftCreatingTool(proposal_store)

    def_dir = tmp_path / "definitions"
    def_dir.mkdir(parents=True, exist_ok=True)
    (def_dir / "proposer.md").write_text(
        "---\nname: proposer\ndisplay_name: Proposer\ndescription: Test agent\n"
        "chat: true\ntools:\n  - create_proposal_tool\n---\nPrompt\n",
        encoding="utf-8",
    )

    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        extra_dirs=[def_dir],
        tools=make_tool_reg(tool),
        event_log=event_log,
    )
    await env.settings.set_default_model("proposer", f"{env.provider}:fake-tools")

    # Script tool call
    fake_openai.state.scripted_tool_calls = [{"name": "create_proposal_tool", "arguments": {}}]

    # Monkeypatch ApprovalGate.decide to fail after the preview has run
    async def failing_decide(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("Gate exploded after preview")

    monkeypatch.setattr(ApprovalGate, "decide", failing_decide)

    sub = env.manager.subscribe(env.project_id, "proposer", after_seq=None)
    await anext(sub)
    await env.manager.send(env.project_id, "proposer", "Run tool")

    events = await collect_until_turn_ended(sub)
    ended = [e for e in events if isinstance(e, TurnEnded)]
    assert len(ended) == 1
    assert ended[0].data.status == "failed"

    # Confirm the draft created in preview was rejected by TurnEnds
    assert tool.created_id is not None
    record = await proposal_store.get(env.project_id, tool.created_id)
    assert record is not None
    assert record.status == "rejected"
    assert record.decided_at is not None

    # Check event log recorded proposal.decided
    decided_events = await event_log.history(env.project_id, types=["proposal.decided"])
    assert len(decided_events) == 1
    assert decided_events[0].payload["proposal_id"] == tool.created_id
    assert decided_events[0].payload["status"] == "rejected"
