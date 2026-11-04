"""Tests for Bash deploy gate (Task 7, #753)."""

from __future__ import annotations

import pytest

from mlops_agents.agents.approvals.authority import ActionCall
from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.settings import set_permission_level
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import ToolContext
from mlops_agents.agents.tools.bash import BashArgs, BashTool
from mlops_agents.credentials.project_secrets import ProjectSecrets
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.ids import new_id
from mlops_agents.monitoring.hooks.service import HooksService
from mlops_agents.proposals.payloads import CommandSpec
from mlops_agents.storage.databases import Databases
from tests.agents.gate_helpers import FakeFileAccess, make_call, setup_session_and_turn
from tests.agents.tools.test_bash import FakeSandbox
from tests.conftest import memory_project_secrets
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


def _spec(
    kind: str = "deploy",
    template: str | None = None,
    placeholders: list[str] | None = None,
    deploys: bool = True,
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


def _tool_context(project_id: str, agent: str = "orchestrator") -> ToolContext:
    return ToolContext(
        project_id=project_id,
        agent=agent,
        session_id="ses_1",
        turn_id="trn_1",
        files=FakeFileAccess(),
        task=None,
        tool_call_id="call_bash_1",
    )


@pytest.mark.asyncio
async def test_deploy_command_through_bash_asks_at_autonomous(
    databases: Databases,
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    spec = _spec(
        kind="deploy",
        template="uv run deploy.py --version {candidate_version}",
        placeholders=["candidate_version"],
        deploys=True,
    )
    hook = await service.register(project_id, spec, "deploy_hook", "churn", "handoff")

    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "autonomous")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    tool = BashTool(hooks=service)
    args = BashArgs(command="uv run deploy.py --version 1.0.0")
    call = make_call(tool, "call_deploy_1", project_id, args=args)

    decisions = await gate.decide([call])
    dec = decisions["call_deploy_1"]
    assert dec.outcome == "ask"
    assert dec.approval_id is not None
    action = await tool.action_call(args, call.ctx)
    assert action is not None and action.hook_id == hook.id


@pytest.mark.asyncio
async def test_non_deploying_hook_command_keeps_run_decision(
    databases: Databases,
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    spec = _spec(
        kind="train",
        template="uv run train.py --model {model}",
        placeholders=["model"],
        deploys=False,
    )
    await service.register(project_id, spec, "train_hook", "churn", "handoff")

    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "autonomous")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    tool = BashTool(hooks=service)
    args = BashArgs(command="uv run train.py --model churn")
    call = make_call(tool, "call_train_1", project_id, args=args)

    decisions = await gate.decide([call])
    dec = decisions["call_train_1"]
    assert dec.outcome == "allow"


@pytest.mark.asyncio
async def test_unmatched_command_keeps_run_decision(
    databases: Databases,
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    spec = _spec(
        kind="deploy",
        template="uv run deploy.py --version {candidate_version}",
        placeholders=["candidate_version"],
        deploys=True,
    )
    await service.register(project_id, spec, "deploy_hook", "churn", "handoff")

    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "autonomous")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    tool = BashTool(hooks=service)
    args = BashArgs(command="ls -la")
    call = make_call(tool, "call_ls_1", project_id, args=args)

    decisions = await gate.decide([call])
    dec = decisions["call_ls_1"]
    assert dec.outcome == "allow"


@pytest.mark.asyncio
async def test_whitespace_normalisation(
    databases: Databases,
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    spec = _spec(
        kind="deploy",
        template="uv   run \n  deploy.py  --version   {candidate_version}",
        placeholders=["candidate_version"],
        deploys=True,
    )
    await service.register(project_id, spec, "deploy_hook", "churn", "handoff")

    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "autonomous")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    tool = BashTool(hooks=service)
    args = BashArgs(command="uv run deploy.py --version 1.0.0")
    call = make_call(tool, "call_ws_1", project_id, args=args)

    decisions = await gate.decide([call])
    dec = decisions["call_ws_1"]
    assert dec.outcome == "ask"


@pytest.mark.asyncio
async def test_no_placeholder_template(
    databases: Databases,
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    spec = _spec(
        kind="deploy",
        template="uv run deploy.sh",
        placeholders=[],
        deploys=True,
    )
    await service.register(project_id, spec, "deploy_hook", "churn", "handoff")

    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "autonomous")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    tool = BashTool(hooks=service)

    # Directly matches
    call1 = make_call(tool, "call_np_1", project_id, args=BashArgs(command="uv run deploy.sh"))
    decisions1 = await gate.decide([call1])
    assert decisions1["call_np_1"].outcome == "ask"

    # Embedded in a wrapper command
    call2 = make_call(
        tool, "call_np_2", project_id, args=BashArgs(command="bash -c 'uv run deploy.sh'")
    )
    decisions2 = await gate.decide([call2])
    assert decisions2["call_np_2"].outcome == "ask"


@pytest.mark.asyncio
async def test_action_call_details_and_disabled_hook(
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    spec = _spec(
        kind="deploy",
        template="uv run deploy.py --version {candidate_version}",
        placeholders=["candidate_version"],
        deploys=True,
    )
    hook = await service.register(project_id, spec, "deploy_hook", "churn", "handoff")

    tool = BashTool(hooks=service)
    ctx = _tool_context(project_id, agent="test_agent")
    args = BashArgs(command="uv run deploy.py --version 2.0.0")

    call = await tool.action_call(args, ctx)
    assert isinstance(call, ActionCall)
    assert call.project_id == project_id
    assert call.agent == "test_agent"
    assert call.kind == "deploy"
    assert call.hook_id == hook.id
    assert call.model == "churn"
    assert call.incident_id is None
    assert call.rule_id is None
    assert call.policy_id is None
    assert call.deploys is None
    assert call.has_production_signal is None
    assert call.team_gate_authority is None

    # Disabled hook is ignored
    await service.set_enabled(project_id, hook.id, False)
    assert await tool.action_call(args, ctx) is None


@pytest.mark.asyncio
async def test_action_call_no_hooks_returns_none() -> None:
    tool = BashTool()
    ctx = _tool_context("prj_test")
    args = BashArgs(command="uv run deploy.py --version 1.0.0")
    assert await tool.action_call(args, ctx) is None


def test_default_registry_passes_hooks_to_bash(service: HooksService) -> None:
    reg = default_registry(sandbox=FakeSandbox(), hooks=service)
    bash_tool = reg.get("Bash")
    assert isinstance(bash_tool, BashTool)
    assert getattr(bash_tool, "_hooks", None) is service
