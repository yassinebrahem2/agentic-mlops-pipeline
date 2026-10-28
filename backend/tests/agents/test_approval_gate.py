"""Tests for ApprovalGate."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, TypedDict, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from pydantic import BaseModel
from sqlalchemy import insert

from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.settings import set_permission_level
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.policy.table import PermissionLevel
from mlops_agents.agents.runtime.hooks import GateCall, GateDecision
from mlops_agents.agents.tools.base import (
    AgentTool,
    FileAccess,
    Preview,
    RiskClass,
    ToolContext,
    ToolOutput,
)
from mlops_agents.clock import now_iso
from mlops_agents.events.audit import SqliteAuditLog
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import sessions, turns


class DummyArgs(BaseModel):
    path: str | None = None
    command: str | None = None
    target: str | None = None


class FakeFileAccess(FileAccess):
    @property
    def root(self) -> Path:
        return Path("/workspace")

    async def tracked(self) -> list[str]:
        return []

    async def under(self, prefix: str) -> list[str]:
        return []

    async def resolve_file(self, user_path: str) -> str:
        return user_path

    async def resolve_dir(self, user_path: str | None) -> str:
        return user_path or ""

    def invalidate(self) -> None:
        pass


class ReadTool(AgentTool[DummyArgs]):
    name: ClassVar[str] = "read_file"
    description: ClassVar[str] = "Dummy read tool"
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = DummyArgs

    async def run(self, ctx: ToolContext, args: DummyArgs) -> ToolOutput:
        return ToolOutput("ok")

    async def preview(self, ctx: ToolContext, args: DummyArgs) -> Preview:
        return Preview(kind="text", title=self.name, body=f"preview for {self.name}")


class WriteTool(AgentTool[DummyArgs]):
    name: ClassVar[str] = "edit_file"
    description: ClassVar[str] = "Dummy write tool"
    risk: ClassVar[RiskClass] = RiskClass.WRITE_CODE
    args_model: ClassVar[type[BaseModel]] = DummyArgs

    async def run(self, ctx: ToolContext, args: DummyArgs) -> ToolOutput:
        return ToolOutput("ok")

    async def preview(self, ctx: ToolContext, args: DummyArgs) -> Preview:
        return Preview(kind="text", title=self.name, body=f"preview for {self.name}")


class RunTool(AgentTool[DummyArgs]):
    name: ClassVar[str] = "bash"
    description: ClassVar[str] = "Dummy run tool"
    risk: ClassVar[RiskClass] = RiskClass.RUN
    args_model: ClassVar[type[BaseModel]] = DummyArgs

    async def run(self, ctx: ToolContext, args: DummyArgs) -> ToolOutput:
        return ToolOutput("ok")

    async def preview(self, ctx: ToolContext, args: DummyArgs) -> Preview:
        return Preview(kind="text", title=self.name, body=f"preview for {self.name}")


class GithubTool(AgentTool[DummyArgs]):
    name: ClassVar[str] = "open_pr"
    description: ClassVar[str] = "Dummy github tool"
    risk: ClassVar[RiskClass] = RiskClass.GITHUB
    args_model: ClassVar[type[BaseModel]] = DummyArgs

    async def run(self, ctx: ToolContext, args: DummyArgs) -> ToolOutput:
        return ToolOutput("ok")

    async def preview(self, ctx: ToolContext, args: DummyArgs) -> Preview:
        return Preview(kind="text", title=self.name, body=f"preview for {self.name}")


class DeployTool(AgentTool[DummyArgs]):
    name: ClassVar[str] = "deploy_tool"
    description: ClassVar[str] = "Dummy deploy tool"
    risk: ClassVar[RiskClass] = RiskClass.DEPLOY
    args_model: ClassVar[type[BaseModel]] = DummyArgs

    async def run(self, ctx: ToolContext, args: DummyArgs) -> ToolOutput:
        return ToolOutput("ok")

    async def preview(self, ctx: ToolContext, args: DummyArgs) -> Preview:
        return Preview(kind="text", title=self.name, body=f"preview for {self.name}")


class ProposeArgs(BaseModel):
    kind: object = None


class ProposeTool(AgentTool[ProposeArgs]):
    """A tool of class propose, as the proposals plan's Propose will be; its name is not read."""

    name: ClassVar[str] = "propose_something"
    description: ClassVar[str] = "Dummy propose tool"
    risk: ClassVar[RiskClass] = RiskClass.PROPOSE
    args_model: ClassVar[type[BaseModel]] = ProposeArgs

    async def run(self, ctx: ToolContext, args: ProposeArgs) -> ToolOutput:
        return ToolOutput("ok")


class _GateState(TypedDict):
    decisions: dict[str, GateDecision]


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


def _make_call(
    tool: AgentTool[Any],
    tool_call_id: str,
    project_id: str,
    turn_id: str = "trn_1",
    args: BaseModel | None = None,
) -> GateCall:
    ctx = ToolContext(
        project_id=project_id,
        agent="orchestrator",
        session_id="ses_1",
        turn_id=turn_id,
        files=FakeFileAccess(),
        task=None,
        tool_call_id=tool_call_id,
    )
    return GateCall(
        tool_call_id=tool_call_id,
        message_id="msg_1",
        tool=tool,
        args=args or DummyArgs(target="test_target"),
        ctx=ctx,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("level", "expected_outcomes"),
    [
        (
            "suggest",
            {
                "call_read": "allow",
                "call_write": "deny",
                "call_run": "deny",
                "call_gh": "ask",
            },
        ),
        (
            "ask",
            {
                "call_read": "allow",
                "call_write": "ask",
                "call_run": "ask",
                "call_gh": "ask",
            },
        ),
        (
            "autonomous",
            {
                "call_read": "allow",
                "call_write": "allow",
                "call_run": "allow",
                "call_gh": "allow",
            },
        ),
        (
            "bypass",
            {
                "call_read": "allow",
                "call_write": "allow",
                "call_run": "allow",
                "call_gh": "allow",
            },
        ),
    ],
)
async def test_gate_decide_across_permission_levels(
    databases: Databases,
    paths: DataPaths,
    level: PermissionLevel,
    expected_outcomes: dict[str, str],
) -> None:
    project_id = f"prj_gate{level}"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await _setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, level)

    store = ApprovalStore(databases)
    audit_log = SqliteAuditLog(databases, secrets=[])
    event_log = SqliteEventLog(databases)
    gate = ApprovalGate(databases, store=store, audit_log=audit_log, event_log=event_log)

    t_read = ReadTool()
    t_write = WriteTool()
    t_run = RunTool()
    t_gh = GithubTool()

    calls = [
        _make_call(t_read, "call_read", project_id),
        _make_call(t_write, "call_write", project_id),
        _make_call(t_run, "call_run", project_id),
        _make_call(t_gh, "call_gh", project_id),
    ]

    decisions = await gate.decide(calls)

    assert set(decisions.keys()) == {"call_read", "call_write", "call_run", "call_gh"}
    for tc_id, expected_outcome in expected_outcomes.items():
        assert decisions[tc_id].outcome == expected_outcome


@pytest.mark.asyncio
async def test_gate_decide_denial_messages(databases: Databases, paths: DataPaths) -> None:
    project_id = "prj_gatedenial"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await _setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "suggest")

    store = ApprovalStore(databases)
    audit_log = SqliteAuditLog(databases, secrets=[])
    event_log = SqliteEventLog(databases)
    gate = ApprovalGate(databases, store=store, audit_log=audit_log, event_log=event_log)

    t_write = WriteTool()
    t_run = RunTool()
    t_deploy = DeployTool()

    calls = [
        _make_call(t_write, "c_w", project_id),
        _make_call(t_run, "c_r", project_id),
        _make_call(t_deploy, "c_d", project_id),
    ]

    decisions = await gate.decide(calls)

    assert decisions["c_w"].outcome == "deny"
    assert decisions["c_w"].message == (
        "Action denied: permission level is 'suggest'. "
        "Propose the change as a diff in your answer instead of modifying files directly."
    )

    assert decisions["c_r"].outcome == "deny"
    assert (
        decisions["c_r"].message
        == "Action denied: permission level 'suggest' denies 'run' actions."
    )

    assert decisions["c_d"].outcome == "deny"
    assert (
        decisions["c_d"].message
        == "Action denied: permission level 'suggest' denies 'deploy' actions."
    )


@pytest.mark.asyncio
async def test_gate_decide_audit_and_event_records(databases: Databases, paths: DataPaths) -> None:
    project_id = "prj_gaterecords"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await _setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "ask")

    store = ApprovalStore(databases)
    audit_log = SqliteAuditLog(databases, secrets=[])
    event_log = SqliteEventLog(databases)
    gate = ApprovalGate(databases, store=store, audit_log=audit_log, event_log=event_log)

    t_read = ReadTool()
    t_write = WriteTool()

    calls = [
        _make_call(t_read, "c_read", project_id, args=DummyArgs(path="main.py")),
        _make_call(t_write, "c_write", project_id, args=DummyArgs(path="model.py")),
    ]

    decisions = await gate.decide(calls)

    assert decisions["c_read"].outcome == "allow"
    assert decisions["c_write"].outcome == "ask"
    assert decisions["c_write"].approval_id is not None
    assert decisions["c_write"].preview is not None

    # Check AuditLog
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        from mlops_agents.storage.tables import audit as audit_table

        rows = (
            (await conn.execute(audit_table.select().order_by(audit_table.c.id.asc())))
            .mappings()
            .all()
        )
        assert len(rows) == 2
        # One allowed, one asked
        row_by_action = {r["action"]: r for r in rows}
        assert row_by_action["read_file"]["decision"] == "allowed"
        assert row_by_action["read_file"]["permission_level"] == "ask"
        assert row_by_action["read_file"]["actor"] == "orchestrator"
        assert row_by_action["read_file"]["target"] == "main.py"

        assert row_by_action["edit_file"]["decision"] == "asked"
        assert row_by_action["edit_file"]["permission_level"] == "ask"
        assert row_by_action["edit_file"]["actor"] == "orchestrator"
        assert row_by_action["edit_file"]["target"] == "model.py"

    # Check EventLog
    events = await event_log.history(project_id, types=["approval.requested"])
    assert len(events) == 1
    assert events[0].source == "orchestrator"
    assert events[0].subject_id == decisions["c_write"].approval_id
    assert events[0].payload == {
        "approval_id": decisions["c_write"].approval_id,
        "turn_id": "trn_1",
        "tool": "edit_file",
    }


@pytest.mark.asyncio
async def test_same_tool_call_id_in_two_turns_gets_two_separate_approvals(
    databases: Databases, paths: DataPaths
) -> None:
    project_id = "prj_twoturns"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await _setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            turns.insert().values(
                id="trn_2",
                session_id="ses_1",
                trigger_message_id="msg_2",
                status="running",
                error=None,
                created_at="2026-10-25T10:00:00Z",
                started_at="2026-10-25T10:00:00Z",
                ended_at=None,
            )
        )
    await set_permission_level(databases, project_id, "ask")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    t_write = WriteTool()
    call1 = _make_call(t_write, "call_0", project_id, turn_id="trn_1")
    dec1 = await gate.decide([call1])
    assert dec1["call_0"].outcome == "ask"
    apr_id_1 = dec1["call_0"].approval_id
    assert apr_id_1 is not None

    call2 = _make_call(t_write, "call_0", project_id, turn_id="trn_2")
    dec2 = await gate.decide([call2])
    assert dec2["call_0"].outcome == "ask"
    apr_id_2 = dec2["call_0"].approval_id
    assert apr_id_2 is not None

    # Must be two separate approvals
    assert apr_id_1 != apr_id_2

    # Second asks, and both records exist in store
    rec1 = await store.get(project_id, apr_id_1)
    rec2 = await store.get(project_id, apr_id_2)
    assert rec1 is not None and rec1.turn_id == "trn_1"
    assert rec2 is not None and rec2.turn_id == "trn_2"
    assert rec2.status == "pending"


@pytest.mark.asyncio
async def test_gate_wait_pending_triggers_interrupt_in_graph(
    databases: Databases, paths: DataPaths
) -> None:
    project_id = "prj_gatewaitinterrupt"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await _setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "ask")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    t_write = WriteTool()
    call = _make_call(t_write, "c_wait_1", project_id)
    decisions = await gate.decide([call])

    # Build a LangGraph test graph with checkpointer
    builder = StateGraph(_GateState)

    async def wait_node(state: _GateState) -> _GateState:
        result = await gate.wait(project_id, "trn_1", state["decisions"])
        return {"decisions": result}

    builder.add_node("wait_step", wait_node)  # pyright: ignore[reportUnknownMemberType]
    builder.add_edge(START, "wait_step")
    checkpointer = MemorySaver()
    graph = builder.compile(checkpointer=checkpointer)  # pyright: ignore[reportUnknownMemberType]

    config: RunnableConfig = {"configurable": {"thread_id": "test_thread_1"}}
    # When approval is pending, wait triggers interrupt
    res = cast(
        dict[str, Any],
        await graph.ainvoke({"decisions": decisions}, config=config),  # pyright: ignore[reportUnknownMemberType]
    )
    assert "__interrupt__" in res


@pytest.mark.asyncio
async def test_gate_wait_decided_returns_mapped_decisions(
    databases: Databases, paths: DataPaths
) -> None:
    project_id = "prj_gatewaitdecided"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await _setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "ask")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    t_write = WriteTool()
    t_run = RunTool()

    calls = [
        _make_call(t_write, "c_w_approved", project_id),
        _make_call(t_run, "c_r_rejected", project_id),
    ]
    decisions = await gate.decide(calls)

    apr_w_id = decisions["c_w_approved"].approval_id
    apr_r_id = decisions["c_r_rejected"].approval_id
    assert apr_w_id is not None
    assert apr_r_id is not None

    # Decide both: approve first, reject second with note
    await store.decide(project_id, apr_w_id, approve=True)
    await store.decide(project_id, apr_r_id, approve=False, note="dangerous command")

    # In a graph, calling wait now returns the updated decisions
    builder = StateGraph(_GateState)

    async def wait_node(state: _GateState) -> _GateState:
        result = await gate.wait(project_id, "trn_1", state["decisions"])
        return {"decisions": result}

    builder.add_node("wait_step", wait_node)  # pyright: ignore[reportUnknownMemberType]
    builder.add_edge(START, "wait_step")
    graph = builder.compile(checkpointer=MemorySaver())  # pyright: ignore[reportUnknownMemberType]

    config: RunnableConfig = {"configurable": {"thread_id": "test_thread_2"}}
    res = cast(
        dict[str, Any],
        await graph.ainvoke({"decisions": decisions}, config=config),  # pyright: ignore[reportUnknownMemberType]
    )

    assert "__interrupt__" not in res
    result_decisions = cast(dict[str, GateDecision], res["decisions"])
    assert result_decisions["c_w_approved"].outcome == "approved"
    assert result_decisions["c_w_approved"].message is None

    assert result_decisions["c_r_rejected"].outcome == "rejected"
    assert (
        result_decisions["c_r_rejected"].message
        == "The user rejected this action. Note: dangerous command"
    )


@pytest.mark.asyncio
async def test_gate_wait_no_asks_returns_immediately(
    databases: Databases, paths: DataPaths
) -> None:
    project_id = "prj_gatewaitnoasks"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await _setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "bypass")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    t_read = ReadTool()
    call = _make_call(t_read, "c_read_bypass", project_id)
    decisions = await gate.decide([call])

    res = await gate.wait(project_id, "trn_1", decisions)
    assert res == decisions


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["suggest", "ask", "autonomous", "bypass"])
async def test_a_propose_call_asks_at_every_level_and_carries_its_proposal_kind(
    databases: Databases, paths: DataPaths, level: PermissionLevel
) -> None:
    project_id = f"prj_propose{level}"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await _setup_session_and_turn(databases, project_id)
    await set_permission_level(databases, project_id, level)
    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    decisions = await gate.decide(
        [
            _make_call(ProposeTool(), "c_prop", project_id, args=ProposeArgs(kind="handoff")),
            _make_call(DeployTool(), "c_deploy", project_id),
        ]
    )

    proposal = decisions["c_prop"]
    assert proposal.outcome == "ask"
    assert proposal.proposal_kind == "handoff"
    assert proposal.approval_id is not None
    stored = await store.get(project_id, proposal.approval_id)
    assert stored is not None
    assert (stored.proposal_kind, stored.risk_class) == ("handoff", RiskClass.PROPOSE)
    # Another class never carries a proposal kind.
    assert decisions["c_deploy"].proposal_kind is None
    if decisions["c_deploy"].approval_id is not None:
        other = await store.get(project_id, decisions["c_deploy"].approval_id)
        assert other is not None and other.proposal_kind is None


@pytest.mark.asyncio
async def test_a_propose_call_whose_kind_is_not_a_string_has_no_proposal_kind(
    databases: Databases, paths: DataPaths
) -> None:
    project_id = "prj_proposebad"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await _setup_session_and_turn(databases, project_id)
    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    decisions = await gate.decide(
        [_make_call(ProposeTool(), "c_prop", project_id, args=ProposeArgs(kind=3))]
    )

    assert decisions["c_prop"].outcome == "ask"
    assert decisions["c_prop"].proposal_kind is None
    approval_id = decisions["c_prop"].approval_id
    assert approval_id is not None
    stored = await store.get(project_id, approval_id)
    assert stored is not None and stored.proposal_kind is None
