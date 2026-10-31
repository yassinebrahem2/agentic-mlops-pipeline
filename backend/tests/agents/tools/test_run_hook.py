"""Tests for RunHookTool (I33)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mlops_agents.agents.approvals.authority import (
    ActionCall,
    Authority,
    AuthorityRegistry,
    GateFacts,
)
from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.settings import set_permission_level
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.tools.base import RiskClass, ToolContext, ToolFailure
from mlops_agents.agents.tools.names import RUN_HOOK
from mlops_agents.agents.tools.run_hook import RunHookArgs, RunHookTool
from mlops_agents.credentials.project_secrets import ProjectSecrets
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.ids import new_id
from mlops_agents.monitoring.hooks.contract import HookResult
from mlops_agents.monitoring.hooks.service import HooksService
from mlops_agents.monitoring.untrusted import untrusted
from mlops_agents.proposals.payloads import CommandSpec
from mlops_agents.storage.databases import Databases
from tests.agents.gate_helpers import FakeFileAccess, make_call, setup_session_and_turn
from tests.conftest import memory_project_secrets
from tests.monitoring.hooks.conftest import FakeRunner
from tests.proposals.samples import command
from tests.providers.fake_clock import FakeClock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def secrets(databases: Databases) -> ProjectSecrets:
    return memory_project_secrets(databases)


@pytest.fixture
def events(databases: Databases) -> SqliteEventLog:
    return SqliteEventLog(databases)


@pytest.fixture
def service(
    databases: Databases,
    secrets: ProjectSecrets,
    events: SqliteEventLog,
    clock: FakeClock,
) -> HooksService:
    return HooksService(databases=databases, secrets=secrets, events=events, clock=clock)


class _StaticFacts(GateFacts):
    def __init__(
        self,
        deploys: bool | None = False,
        has_prod: bool | None = True,
        team_gate: bool | None = True,
    ) -> None:
        self._deploys = deploys
        self._has_prod = has_prod
        self._team_gate = team_gate

    async def facts(
        self, project_id: str, hook_id: str | None
    ) -> tuple[bool | None, bool | None, bool | None]:
        return (self._deploys, self._has_prod, self._team_gate)


def _spec(
    kind: str = "deploy",
    template: str | None = None,
    placeholders: list[str] | None = None,
    deploys: bool = False,
) -> CommandSpec:
    tmpl = template or "echo ok"
    phs = placeholders if placeholders is not None else []
    return CommandSpec.model_validate(
        command(
            kind=kind,
            type="command",
            template=tmpl,
            placeholders=phs,
            deploys=deploys,
        )
    )


def _tool_context(
    project_id: str,
    authority: Authority | None = None,
    agent: str = "orchestrator",
) -> ToolContext:
    return ToolContext(
        project_id=project_id,
        agent=agent,
        session_id="ses_1",
        turn_id="trn_1",
        files=FakeFileAccess(),
        task=None,
        tool_call_id="call_hook_1",
        authority=authority,
    )


@pytest.mark.asyncio
async def test_run_hook_tool_attributes(service: HooksService) -> None:
    tool = RunHookTool(service)
    assert tool.name == RUN_HOOK
    assert tool.name == "RunHook"
    assert tool.risk == RiskClass.DEPLOY
    assert tool.description


@pytest.mark.asyncio
async def test_action_call_rollback_hook_with_rule(
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    spec = _spec(kind="rollback", deploys=False)
    hook = await service.register(project_id, spec, "rollback_hook", "churn", "handoff")

    tool = RunHookTool(service)
    ctx = _tool_context(project_id, agent="test_agent")
    args = RunHookArgs(
        hook_id=hook.id,
        reason="Rollback due to drift",
        incident_id="inc_42",
        rule_id="rul_1",
    )

    call = await tool.action_call(args, ctx)
    assert isinstance(call, ActionCall)
    assert call.project_id == project_id
    assert call.agent == "test_agent"
    assert call.kind == "rollback"
    assert call.hook_id == hook.id
    assert call.model == "churn"
    assert call.incident_id == "inc_42"
    assert call.rule_id == "rul_1"
    assert call.policy_id is None
    # Gate fills these, never from the agent
    assert call.deploys is None
    assert call.has_production_signal is None
    assert call.team_gate_authority is None


def test_agent_supplied_deploys_is_forbidden() -> None:
    with pytest.raises(ValidationError):
        RunHookArgs.model_validate({"hook_id": "hok_1", "reason": "test", "deploys": False})


@pytest.mark.asyncio
async def test_action_call_unknown_hook_raises_tool_failure(
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    tool = RunHookTool(service)
    ctx = _tool_context(project_id)
    args = RunHookArgs(hook_id="hok_nonexistent", reason="test")

    with pytest.raises(ToolFailure, match="hok_nonexistent"):
        await tool.action_call(args, ctx)


@pytest.mark.asyncio
async def test_run_without_authority_raises(
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    spec = _spec(kind="deploy")
    hook = await service.register(project_id, spec, "deploy_hook", "churn", "handoff")

    tool = RunHookTool(service)
    ctx = _tool_context(project_id, authority=None)
    args = RunHookArgs(hook_id=hook.id, reason="deploy model")

    with pytest.raises(RuntimeError, match="RunHook reached without authority"):
        await tool.run(ctx, args)


@pytest.mark.asyncio
async def test_run_undeclared_params_refused(
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    spec = _spec(
        kind="train",
        template="python train.py --model {model}",
        placeholders=["model"],
    )
    hook = await service.register(project_id, spec, "train_hook", "churn", "handoff")

    tool = RunHookTool(service)
    auth = Authority(basis="policy", reason="authorized")
    ctx = _tool_context(project_id, authority=auth)

    # Param "unknown_param" is not in hook.spec.placeholders
    args = RunHookArgs(
        hook_id=hook.id,
        reason="train",
        params={"model": "churn", "unknown_param": "foo"},
    )
    with pytest.raises(ToolFailure, match="unknown_param"):
        await tool.run(ctx, args)


@pytest.mark.asyncio
async def test_run_failing_hook_returns_tool_error_and_shows_on_timeline(
    service: HooksService,
    events: SqliteEventLog,
) -> None:
    project_id = new_id("prj")
    runner = FakeRunner(result=HookResult(ok=False, detail="process crashed with exit code 137"))
    service.add_runner(runner)

    spec = _spec(kind="deploy")
    hook = await service.register(project_id, spec, "deploy_hook", "churn", "handoff")

    tool = RunHookTool(service)
    auth = Authority(basis="approval", reason="approved by user")
    ctx = _tool_context(project_id, authority=auth)

    incident_id = "inc_failure_test"
    args = RunHookArgs(
        hook_id=hook.id,
        reason="deploy model",
        incident_id=incident_id,
    )

    output = await tool.run(ctx, args)
    assert output.is_error is True
    assert hook.id in output.content
    expected_detail = untrusted("process crashed with exit code 137", source="hook_run")
    assert expected_detail in output.content

    # Check incident timeline: event log holds hook.failed with subject_id == incident_id
    history = await events.history(project_id, types=["hook.failed"])
    assert len(history) == 1
    assert history[0].subject_id == incident_id
    assert history[0].type == "hook.failed"
    assert history[0].payload["hook_id"] == hook.id


@pytest.mark.asyncio
async def test_run_successful_hook_returns_untrusted_detail(
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    runner = FakeRunner(result=HookResult(ok=True, detail="model version 2 deployed successfully"))
    service.add_runner(runner)

    spec = _spec(kind="deploy")
    hook = await service.register(project_id, spec, "deploy_hook", "churn", "handoff")

    tool = RunHookTool(service)
    auth = Authority(basis="approval", reason="approved")
    ctx = _tool_context(project_id, authority=auth)

    args = RunHookArgs(hook_id=hook.id, reason="deploy")
    output = await tool.run(ctx, args)

    assert output.is_error is False
    assert output.content == untrusted("model version 2 deployed successfully", source="hook_run")


@pytest.mark.asyncio
async def test_run_hook_refused_returns_error(
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    # No runner added -> service.run raises HookRefused("No runner registered...")
    spec = _spec(kind="deploy")
    hook = await service.register(project_id, spec, "deploy_hook", "churn", "handoff")

    tool = RunHookTool(service)
    auth = Authority(basis="approval", reason="approved")
    ctx = _tool_context(project_id, authority=auth)

    args = RunHookArgs(hook_id=hook.id, reason="deploy")
    output = await tool.run(ctx, args)
    assert output.is_error is True
    assert (
        f"Hook '{hook.id}' refused" in output.content
        or f"Hook {hook.id!r} refused" in output.content
    )


@pytest.mark.asyncio
async def test_real_gate_deploy_hook_at_autonomous_asks_and_runs_on_approval(
    databases: Databases,
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    runner = FakeRunner(result=HookResult(ok=True, detail="deployed to prod"))
    service.add_runner(runner)

    spec = _spec(kind="deploy")
    hook = await service.register(project_id, spec, "deploy_hook", "churn", "handoff")

    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "autonomous")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    tool = RunHookTool(service)
    args = RunHookArgs(hook_id=hook.id, reason="deploying new version")
    call = make_call(tool, "call_deploy_1", project_id, args=args)

    # 1. Gate asks at autonomous
    decisions = await gate.decide([call])
    dec = decisions["call_deploy_1"]
    assert dec.outcome == "ask"
    assert dec.approval_id is not None
    assert len(runner.calls) == 0

    # 2. User approves the request
    await SessionStore(databases).set_turn(project_id, "trn_1", "waiting")
    await store.decide(project_id, dec.approval_id, approve=True, note="looks good")

    # 3. Resume wait
    resumed_decisions = await gate.wait(project_id, "trn_1", decisions)
    res_dec = resumed_decisions["call_deploy_1"]
    assert res_dec.outcome == "approved"
    assert res_dec.authority is not None
    assert res_dec.authority.basis == "approval"

    # 4. Run tool with the approved authority
    ctx = _tool_context(project_id, authority=res_dec.authority)
    output = await tool.run(ctx, args)
    assert output.is_error is False
    assert len(runner.calls) == 1
    _, _, _, runner_auth = runner.calls[0]
    assert runner_auth.basis == "approval"


@pytest.mark.asyncio
async def test_real_gate_rollback_no_citation_asks_at_ask_and_runs_at_bypass(
    databases: Databases,
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    runner = FakeRunner(result=HookResult(ok=True, detail="rollback successful"))
    service.add_runner(runner)

    spec = _spec(kind="rollback")
    hook = await service.register(project_id, spec, "rollback_hook", "churn", "handoff")

    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")

    authorities = AuthorityRegistry()
    authorities.set_facts(_StaticFacts(deploys=False))
    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store, authorities=authorities)

    tool = RunHookTool(service)
    args = RunHookArgs(hook_id=hook.id, reason="rollback model")
    call = make_call(tool, "call_rb_1", project_id, args=args)

    # At "ask": asks
    await set_permission_level(databases, project_id, "ask")
    dec_ask = await gate.decide([call])
    assert dec_ask["call_rb_1"].outcome == "ask"

    # At "bypass": runs (allowed)
    await set_permission_level(databases, project_id, "bypass")
    dec_byp = await gate.decide([call])
    assert dec_byp["call_rb_1"].outcome == "allow"
    assert dec_byp["call_rb_1"].authority is not None
    assert dec_byp["call_rb_1"].authority.basis == "none"

    ctx = _tool_context(project_id, authority=dec_byp["call_rb_1"].authority)
    output = await tool.run(ctx, args)
    assert output.is_error is False
    assert len(runner.calls) == 1


def test_default_registry_run_hook(service: HooksService) -> None:
    from mlops_agents.agents.tools import default_registry

    reg_without = default_registry()
    assert "RunHook" not in reg_without.names()

    reg_with = default_registry(hooks=service)
    assert "RunHook" in reg_with.names()
    assert isinstance(reg_with.get("RunHook"), RunHookTool)
