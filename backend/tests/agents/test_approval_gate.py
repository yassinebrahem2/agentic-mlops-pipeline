"""Tests for ApprovalGate decisions and proposals."""

from __future__ import annotations

import pytest

from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.settings import set_permission_level
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.policy.table import PermissionLevel
from mlops_agents.agents.tools.base import RiskClass
from mlops_agents.events.audit import SqliteAuditLog
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import turns
from tests.agents.gate_helpers import (
    DeployTool,
    DummyArgs,
    FailingPreviewTool,
    GithubTool,
    ProposeArgs,
    ProposeTool,
    ReadTool,
    RunTool,
    WriteTool,
    make_call,
    setup_session_and_turn,
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
    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
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
        make_call(t_read, "call_read", project_id),
        make_call(t_write, "call_write", project_id),
        make_call(t_run, "call_run", project_id),
        make_call(t_gh, "call_gh", project_id),
    ]

    decisions = await gate.decide(calls)

    assert set(decisions.keys()) == {"call_read", "call_write", "call_run", "call_gh"}
    for tc_id, expected_outcome in expected_outcomes.items():
        assert decisions[tc_id].outcome == expected_outcome


@pytest.mark.asyncio
async def test_gate_decide_denial_messages(databases: Databases, paths: DataPaths) -> None:
    project_id = "prj_gatedenial"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "suggest")

    store = ApprovalStore(databases)
    audit_log = SqliteAuditLog(databases, secrets=[])
    event_log = SqliteEventLog(databases)
    gate = ApprovalGate(databases, store=store, audit_log=audit_log, event_log=event_log)

    t_write = WriteTool()
    t_run = RunTool()
    t_deploy = DeployTool()

    calls = [
        make_call(t_write, "c_w", project_id),
        make_call(t_run, "c_r", project_id),
        make_call(t_deploy, "c_d", project_id),
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
    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "ask")

    store = ApprovalStore(databases)
    audit_log = SqliteAuditLog(databases, secrets=[])
    event_log = SqliteEventLog(databases)
    gate = ApprovalGate(databases, store=store, audit_log=audit_log, event_log=event_log)

    t_read = ReadTool()
    t_write = WriteTool()

    calls = [
        make_call(t_read, "c_read", project_id, args=DummyArgs(path="main.py")),
        make_call(t_write, "c_write", project_id, args=DummyArgs(path="model.py")),
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
    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
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
    call1 = make_call(t_write, "call_0", project_id, turn_id="trn_1")
    dec1 = await gate.decide([call1])
    assert dec1["call_0"].outcome == "ask"
    apr_id_1 = dec1["call_0"].approval_id
    assert apr_id_1 is not None

    call2 = make_call(t_write, "call_0", project_id, turn_id="trn_2")
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
@pytest.mark.parametrize("level", ["suggest", "ask", "autonomous", "bypass"])
async def test_a_propose_call_asks_at_every_level_and_carries_its_proposal_kind(
    databases: Databases, paths: DataPaths, level: PermissionLevel
) -> None:
    project_id = f"prj_propose{level}"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await setup_session_and_turn(databases, project_id)
    await set_permission_level(databases, project_id, level)
    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    decisions = await gate.decide(
        [
            make_call(ProposeTool(), "c_prop", project_id, args=ProposeArgs(kind="handoff")),
            make_call(DeployTool(), "c_deploy", project_id),
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
    await setup_session_and_turn(databases, project_id)
    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    decisions = await gate.decide(
        [make_call(ProposeTool(), "c_prop", project_id, args=ProposeArgs(kind=3))]
    )

    assert decisions["c_prop"].outcome == "ask"
    assert decisions["c_prop"].proposal_kind is None
    approval_id = decisions["c_prop"].approval_id
    assert approval_id is not None
    stored = await store.get(project_id, approval_id)
    assert stored is not None and stored.proposal_kind is None


@pytest.mark.asyncio
async def test_preview_tool_failure_in_decide_returns_deny(
    databases: Databases, paths: DataPaths
) -> None:
    project_id = "prj_previewfail"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await setup_session_and_turn(databases, project_id)
    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    decisions = await gate.decide([make_call(FailingPreviewTool(), "c_fail", project_id)])

    decision = decisions["c_fail"]
    assert decision.outcome == "deny"
    assert decision.message == "Docker is unavailable"
    assert decision.approval_id is None
    pending = await store.pending(project_id)
    assert pending == []
