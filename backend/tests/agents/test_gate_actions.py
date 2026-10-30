"""Tests for action permissions and the gate's second input (I30)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Literal, TypedDict, cast

import aiosqlite
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.graph import START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from pydantic import BaseModel

from mlops_agents.agents.approvals.authority import (
    ActionCall,
    ActionKind,
    Authority,
    AuthorityRegistry,
    GateFacts,
    NoGateFacts,
)
from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.settings import set_permission_level
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.policy.actions import decide_action
from mlops_agents.agents.policy.table import PermissionLevel, PolicyDecision
from mlops_agents.agents.runtime.graph import turn_checkpointer
from mlops_agents.agents.runtime.hooks import GateDecision
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.runtime.tool_step import ToolStep
from mlops_agents.agents.tasks.contract import TaskWorkspaces
from mlops_agents.agents.tools.base import (
    AgentTool,
    RiskClass,
    ToolContext,
    ToolOutput,
)
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.gate_helpers import (
    FakeFileAccess,
    make_call,
    setup_session_and_turn,
)

# --- 1. decide_action Unit Tests Covering the Entire I30 Matrix ---


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("suggest", "deny"),
        ("ask", "ask"),
        ("autonomous", "ask"),
        ("bypass", "allow"),
    ],
)
@pytest.mark.parametrize("kind", ["deploy", "merge_pr"])
def test_decide_action_deploy_and_merge_pr(
    level: PermissionLevel, expected: PolicyDecision, kind: ActionKind
) -> None:
    call = ActionCall(project_id="p1", agent="a1", kind=kind)
    auth = Authority(basis="none", reason="none")
    assert decide_action(level, call, auth) == expected


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("suggest", "ask"),
        ("ask", "allow"),
        ("autonomous", "allow"),
        ("bypass", "allow"),
    ],
)
def test_decide_action_hold(level: PermissionLevel, expected: PolicyDecision) -> None:
    call = ActionCall(project_id="p1", agent="a1", kind="hold")
    auth = Authority(basis="none", reason="none")
    assert decide_action(level, call, auth) == expected


@pytest.mark.parametrize(
    ("level", "basis", "expected"),
    [
        ("suggest", "policy", "deny"),
        ("ask", "policy", "allow"),
        ("autonomous", "policy", "allow"),
        ("bypass", "policy", "allow"),
        ("suggest", "none", "deny"),
        ("ask", "none", "ask"),
        ("autonomous", "none", "ask"),
        ("bypass", "none", "ask"),
    ],
)
@pytest.mark.parametrize("kind", ["train", "evaluate"])
def test_decide_action_train_evaluate_not_deploying(
    level: PermissionLevel,
    basis: Literal["policy", "none"],
    expected: PolicyDecision,
    kind: ActionKind,
) -> None:
    call = ActionCall(project_id="p1", agent="a1", kind=kind, deploys=False)
    auth = Authority(basis=basis, reason="r")
    assert decide_action(level, call, auth) == expected


@pytest.mark.parametrize(
    ("level", "team_gate", "prod_signal", "basis", "expected"),
    [
        # All conditions met -> permissive
        ("suggest", True, True, "policy", "deny"),
        ("ask", True, True, "policy", "allow"),
        ("autonomous", True, True, "policy", "allow"),
        ("bypass", True, True, "policy", "allow"),
        # Missing team_gate_authority
        ("bypass", False, True, "policy", "ask"),
        ("bypass", None, True, "policy", "ask"),
        # Missing production signal
        ("bypass", True, False, "policy", "ask"),
        ("bypass", True, None, "policy", "ask"),
        # Basis is not policy
        ("bypass", True, True, "none", "ask"),
        ("bypass", True, True, "rule", "ask"),
        # All None facts
        ("ask", None, None, "none", "ask"),
        ("autonomous", None, None, "none", "ask"),
        ("bypass", None, None, "none", "ask"),
    ],
)
@pytest.mark.parametrize("kind", ["train", "evaluate"])
def test_decide_action_train_evaluate_run_that_deploys(
    level: PermissionLevel,
    team_gate: bool | None,
    prod_signal: bool | None,
    basis: Literal["rule", "policy", "approval", "none"],
    expected: PolicyDecision,
    kind: ActionKind,
) -> None:
    # When deploys is True or None, it counts as "a run that deploys"
    call = ActionCall(
        project_id="p1",
        agent="a1",
        kind=kind,
        deploys=True,
        team_gate_authority=team_gate,
        has_production_signal=prod_signal,
    )
    auth = Authority(basis=basis, reason="r")
    assert decide_action(level, call, auth) == expected

    call_none_deploys = ActionCall(
        project_id="p1",
        agent="a1",
        kind=kind,
        deploys=None,
        team_gate_authority=team_gate,
        has_production_signal=prod_signal,
    )
    assert decide_action(level, call_none_deploys, auth) == expected


@pytest.mark.parametrize(
    ("level", "basis", "expected"),
    [
        ("suggest", "rule", "deny"),
        ("ask", "rule", "allow"),
        ("autonomous", "rule", "allow"),
        ("bypass", "rule", "allow"),
        ("suggest", "none", "deny"),
        ("ask", "none", "ask"),
        ("autonomous", "none", "ask"),
        ("bypass", "none", "allow"),
    ],
)
@pytest.mark.parametrize("kind", ["rollback", "rerun", "fall_back"])
def test_decide_action_rollback_rerun_fallback_normal(
    level: PermissionLevel,
    basis: Literal["rule", "none"],
    expected: PolicyDecision,
    kind: ActionKind,
) -> None:
    call = ActionCall(project_id="p1", agent="a1", kind=kind, deploys=False)
    auth = Authority(basis=basis, reason="r")
    assert decide_action(level, call, auth) == expected


@pytest.mark.parametrize(
    ("level", "team_gate", "prod_signal", "basis", "expected"),
    [
        ("suggest", True, True, "policy", "deny"),
        ("ask", True, True, "policy", "allow"),
        ("autonomous", True, True, "policy", "allow"),
        ("bypass", True, True, "policy", "allow"),
        ("bypass", None, True, "policy", "ask"),
        ("bypass", True, None, "policy", "ask"),
        ("bypass", True, True, "none", "ask"),
        ("bypass", None, None, "none", "ask"),
    ],
)
@pytest.mark.parametrize("kind", ["rollback", "rerun", "fall_back"])
def test_decide_action_rollback_rerun_fallback_suspended_hook(
    level: PermissionLevel,
    team_gate: bool | None,
    prod_signal: bool | None,
    basis: Literal["rule", "policy", "approval", "none"],
    expected: PolicyDecision,
    kind: ActionKind,
) -> None:
    # A suspended hook has deploys=True -> takes "a run that deploys" rows
    call = ActionCall(
        project_id="p1",
        agent="a1",
        kind=kind,
        deploys=True,
        team_gate_authority=team_gate,
        has_production_signal=prod_signal,
    )
    auth = Authority(basis=basis, reason="r")
    assert decide_action(level, call, auth) == expected


# --- 2. Gate Integration Tests ---


class _FakeActionArgs(BaseModel):
    rule_id: str | None = None
    policy_id: str | None = None
    deploys_claim: bool | None = None


class _FakeActionTool(AgentTool[_FakeActionArgs]):
    name: ClassVar[str] = "fake_action_tool"
    description: ClassVar[str] = "Fake action tool"
    risk: ClassVar[RiskClass] = RiskClass.DEPLOY
    args_model: ClassVar[type[BaseModel]] = _FakeActionArgs

    def __init__(self, kind: ActionKind) -> None:
        self.kind = kind
        self.last_ctx: ToolContext | None = None

    async def run(self, ctx: ToolContext, args: _FakeActionArgs) -> ToolOutput:
        self.last_ctx = ctx
        return ToolOutput("executed")

    async def action_call(self, args: Any, ctx: ToolContext) -> ActionCall | None:
        return ActionCall(
            project_id=ctx.project_id,
            agent=ctx.agent,
            kind=self.kind,
            rule_id=getattr(args, "rule_id", None),
            policy_id=getattr(args, "policy_id", None),
            deploys=getattr(args, "deploys_claim", None),
        )


class _FakeProposeArgs(BaseModel):
    kind: str


class _FakeProposeTool(AgentTool[_FakeProposeArgs]):
    name: ClassVar[str] = "fake_propose"
    description: ClassVar[str] = "Fake propose"
    risk: ClassVar[RiskClass] = RiskClass.PROPOSE
    args_model: ClassVar[type[BaseModel]] = _FakeProposeArgs

    async def run(self, ctx: ToolContext, args: _FakeProposeArgs) -> ToolOutput:
        return ToolOutput("proposed")


class _StaticFacts(GateFacts):
    def __init__(self, deploys: bool | None, has_prod: bool | None, team_gate: bool | None) -> None:
        self._res = (deploys, has_prod, team_gate)

    async def facts(
        self, project_id: str, hook_id: str | None
    ) -> tuple[bool | None, bool | None, bool | None]:
        return self._res


class _StaticAuthority:
    def __init__(self, basis: Literal["rule", "policy", "none"]) -> None:
        self.basis = basis

    async def authorize(self, call: ActionCall) -> Authority:
        return Authority(basis=self.basis, reason="test authorization")


@pytest.mark.asyncio
async def test_authority_registry_register_twice_raises() -> None:
    reg = AuthorityRegistry()
    reg.register("rule", _StaticAuthority("rule"))
    with pytest.raises(ValueError, match="already registered"):
        reg.register("rule", _StaticAuthority("rule"))


@pytest.mark.asyncio
async def test_authority_registry_set_facts_replaces_default() -> None:
    reg = AuthorityRegistry()
    assert isinstance(reg.facts, NoGateFacts)
    assert await reg.facts.facts("p1", None) == (None, None, None)

    reg.set_facts(_StaticFacts(True, False, True))
    assert await reg.facts.facts("p1", None) == (True, False, True)


@pytest.mark.asyncio
async def test_gate_facts_overwrites_tool_claim(databases: Databases, paths: DataPaths) -> None:
    project_id = "prj_factsoverwrite"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await setup_session_and_turn(databases, project_id)
    await set_permission_level(databases, project_id, "bypass")

    # GateFacts says deploys=True, has_prod=False, team_gate=False
    authorities = AuthorityRegistry()
    authorities.set_facts(_StaticFacts(True, False, False))

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store, authorities=authorities)

    # Tool claims deploys_claim=False
    tool = _FakeActionTool(kind="rollback")
    call = make_call(
        tool,
        "call_1",
        project_id,
        args=_FakeActionArgs(deploys_claim=False),
    )
    # If the claim deploys_claim=False was trusted, rollback at bypass with basis=none would be allowed.
    # But GateFacts overwrites deploys=True -> suspended hook -> asks!
    decisions = await gate.decide([call])
    assert decisions["call_1"].outcome == "ask"


@pytest.mark.asyncio
async def test_gate_rule_authority_allows_rollback_at_ask(
    databases: Databases, paths: DataPaths
) -> None:
    project_id = "prj_rulerollback"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await setup_session_and_turn(databases, project_id)
    await set_permission_level(databases, project_id, "ask")

    authorities = AuthorityRegistry()
    authorities.set_facts(_StaticFacts(False, False, False))
    authorities.register("rule", _StaticAuthority("rule"))

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store, authorities=authorities)

    tool = _FakeActionTool(kind="rollback")
    call = make_call(tool, "call_rule", project_id, args=_FakeActionArgs(rule_id="rul_1"))
    decisions = await gate.decide([call])
    assert decisions["call_rule"].outcome == "allow"
    assert decisions["call_rule"].authority is not None
    assert decisions["call_rule"].authority.basis == "rule"


@pytest.mark.asyncio
async def test_gate_rule_authority_none_asks_at_ask(databases: Databases, paths: DataPaths) -> None:
    project_id = "prj_rulenone"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await setup_session_and_turn(databases, project_id)
    await set_permission_level(databases, project_id, "ask")

    authorities = AuthorityRegistry()
    authorities.set_facts(_StaticFacts(False, False, False))
    authorities.register("rule", _StaticAuthority("none"))

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store, authorities=authorities)

    tool = _FakeActionTool(kind="rollback")
    call = make_call(tool, "call_none", project_id, args=_FakeActionArgs(rule_id="rul_1"))
    decisions = await gate.decide([call])
    assert decisions["call_none"].outcome == "ask"


@pytest.mark.asyncio
async def test_gate_policy_id_on_rollback_is_ignored(
    databases: Databases, paths: DataPaths
) -> None:
    project_id = "prj_rollbackpolignored"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await setup_session_and_turn(databases, project_id)
    await set_permission_level(databases, project_id, "ask")

    authorities = AuthorityRegistry()
    authorities.set_facts(_StaticFacts(False, False, False))
    authorities.register("policy", _StaticAuthority("policy"))

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store, authorities=authorities)

    tool = _FakeActionTool(kind="rollback")
    # policy_id provided on rollback should be ignored; citation for rollback is rule_id only
    call = make_call(tool, "call_ign", project_id, args=_FakeActionArgs(policy_id="pol_1"))
    decisions = await gate.decide([call])
    # basis is none -> asks at level ask
    assert decisions["call_ign"].outcome == "ask"


@pytest.mark.asyncio
async def test_gate_merge_pr_and_hold(databases: Databases, paths: DataPaths) -> None:
    project_id = "prj_mergehold"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await setup_session_and_turn(databases, project_id)

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    tool_merge = _FakeActionTool(kind="merge_pr")
    tool_hold = _FakeActionTool(kind="hold")

    # merge_pr at autonomous -> asks
    await set_permission_level(databases, project_id, "autonomous")
    call_merge_auto = make_call(tool_merge, "c_m_auto", project_id)
    dec_m_auto = await gate.decide([call_merge_auto])
    assert dec_m_auto["c_m_auto"].outcome == "ask"

    # merge_pr at bypass -> allows
    await set_permission_level(databases, project_id, "bypass")
    call_merge_by = make_call(tool_merge, "c_m_by", project_id)
    dec_m_by = await gate.decide([call_merge_by])
    assert dec_m_by["c_m_by"].outcome == "allow"

    # hold at suggest -> asks
    await set_permission_level(databases, project_id, "suggest")
    call_hold_sug = make_call(tool_hold, "c_h_sug", project_id)
    dec_h_sug = await gate.decide([call_hold_sug])
    assert dec_h_sug["c_h_sug"].outcome == "ask"


@pytest.mark.asyncio
async def test_gate_bypass_propose_overrides(databases: Databases, paths: DataPaths) -> None:
    project_id = "prj_proposeoverrides"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await setup_session_and_turn(databases, project_id)
    await set_permission_level(databases, project_id, "bypass")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    tool = _FakeProposeTool()
    call_promo = make_call(tool, "c_promo", project_id, args=_FakeProposeArgs(kind="promotion"))
    call_rule = make_call(tool, "c_rule", project_id, args=_FakeProposeArgs(kind="rule"))

    decisions = await gate.decide([call_promo, call_rule])
    assert decisions["c_promo"].outcome == "allow"
    assert decisions["c_rule"].outcome == "ask"


class _CheckpointerState(TypedDict):
    decisions: dict[str, GateDecision]


@pytest.mark.asyncio
async def test_approved_ask_gives_approval_authority_surviving_checkpoint(
    databases: Databases, paths: DataPaths, tmp_path: Path
) -> None:
    project_id = "prj_approvalauthority"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "ask")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    tool = _FakeActionTool(kind="deploy")
    call = make_call(tool, "c_deploy", project_id)
    decisions = await gate.decide([call])

    apr_id = decisions["c_deploy"].approval_id
    assert apr_id is not None
    assert decisions["c_deploy"].authority is not None
    assert decisions["c_deploy"].authority.basis == "none"

    # User approves the request
    await SessionStore(databases).set_turn(project_id, "trn_1", "waiting")
    await store.decide(project_id, apr_id, approve=True, note="approved for deployment")

    # Resume wait in a LangGraph graph using turn_checkpointer
    builder = StateGraph(_CheckpointerState)

    async def wait_node(state: _CheckpointerState) -> _CheckpointerState:
        result = await gate.wait(project_id, "trn_1", state["decisions"])
        return {"decisions": result}

    builder.add_node("wait_step", wait_node)  # pyright: ignore[reportUnknownMemberType]
    builder.add_edge(START, "wait_step")

    checkpoint_file = tmp_path / "checkpoints.db"
    async with aiosqlite.connect(checkpoint_file) as conn:
        saver = turn_checkpointer(conn)
        await saver.setup()
        graph = builder.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]

        config: RunnableConfig = {"configurable": {"thread_id": "thread_gate_checkpoint"}}
        res = cast(
            dict[str, Any],
            await graph.ainvoke({"decisions": decisions}, config=config),  # pyright: ignore[reportUnknownMemberType]
        )

        res_decisions = cast(dict[str, GateDecision], res["decisions"])
        approved_dec = res_decisions["c_deploy"]
        assert approved_dec.outcome == "approved"
        assert approved_dec.authority is not None
        assert approved_dec.authority.basis == "approval"
        assert approved_dec.authority.ref == apr_id

        # Verify deserialization from checkpointer:
        state_tuple = await saver.aget_tuple(config)
        assert state_tuple is not None
        checkpoint_decisions = state_tuple.checkpoint["channel_values"]["decisions"]
        restored_dec = checkpoint_decisions["c_deploy"]
        assert isinstance(restored_dec, GateDecision)
        assert isinstance(restored_dec.authority, Authority)
        assert restored_dec.authority.basis == "approval"
        assert restored_dec.authority.ref == apr_id


class _DummyWorkspaces(TaskWorkspaces):
    async def current(self, project_id: str, session_id: str) -> Any:
        return None

    async def files(self, project_id: str, task: Any) -> Any:
        return FakeFileAccess()


class _DummySink:
    async def tool_finished(self, message_id: str, result: Any) -> None:
        pass


class _DummyLock:
    async def __aenter__(self) -> None:
        pass

    async def __aexit__(self, *args: Any) -> None:
        pass


@pytest.mark.asyncio
async def test_tool_step_passes_authority_to_context(
    databases: Databases, paths: DataPaths
) -> None:
    project_id = "prj_toolstepauthority"
    paths.project_dir(project_id).mkdir(parents=True, exist_ok=True)
    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    tool = _FakeActionTool(kind="rollback")
    agent = cast(
        Any,
        type(
            "FakeAgent",
            (),
            {
                "definition": type("FakeDef", (), {"name": "orchestrator"})(),
                "files": FakeFileAccess(),
                "tools": [tool],
            },
        )(),
    )

    async def _update_parts() -> None:
        pass

    step = ToolStep(
        agent=agent,
        gate=gate,
        workspaces=_DummyWorkspaces(),
        sink=_DummySink(),
        project_id=project_id,
        session_id="ses_1",
        turn_id="trn_1",
        message_id="msg_1",
        current_parts=[],
        lock=_DummyLock(),
        update_parts=_update_parts,
        approval_store=store,
    )

    expected_auth = Authority(basis="rule", ref="rul_1", reason="matched rule", evidence=[])
    decision = GateDecision(outcome="allow", authority=expected_auth)

    output = await step._run_decided(
        tool,
        {"id": "call_1", "name": tool.name, "args": {}},
        decision,
    )
    assert output.content == "executed"
    assert tool.last_ctx is not None
    assert tool.last_ctx.authority == expected_auth
