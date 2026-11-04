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
    # Template has extra whitespace, command is simple
    args1 = BashArgs(command="uv run deploy.py --version 1.0.0")
    call1 = make_call(tool, "call_ws_1", project_id, args=args1)
    decisions1 = await gate.decide([call1])
    assert decisions1["call_ws_1"].outcome == "ask"

    # Command has extra spaces, tabs, and newlines
    args2 = BashArgs(command="uv \t run\n  deploy.py\t\t--version \n 1.0.0")
    call2 = make_call(tool, "call_ws_2", project_id, args=args2)
    decisions2 = await gate.decide([call2])
    assert decisions2["call_ws_2"].outcome == "ask"


@pytest.mark.asyncio
async def test_line_continuation_in_command_and_template(
    databases: Databases,
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    # 1. Normal template on one line
    spec1 = _spec(
        kind="deploy",
        template="uv run deploy.py --version {candidate_version}",
        placeholders=["candidate_version"],
        deploys=True,
    )
    await service.register(project_id, spec1, "deploy_hook_1", "churn", "handoff")

    # 2. Template stored with backslash line continuations (\n and \r\n)
    spec2 = _spec(
        kind="deploy",
        template="uv run deploy_alt.py \\\n  --version {candidate_version}",
        placeholders=["candidate_version"],
        deploys=True,
    )
    await service.register(project_id, spec2, "deploy_hook_2", "churn", "handoff")

    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "autonomous")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)
    tool = BashTool(hooks=service)

    # Command with \ + \n line continuation matching one-line template
    cmd_cont_lf = "uv run deploy.py \\\n  --version 1.2"
    call_lf = make_call(tool, "call_cont_lf", project_id, args=BashArgs(command=cmd_cont_lf))
    dec_lf = await gate.decide([call_lf])
    assert dec_lf["call_cont_lf"].outcome == "ask"

    # Command with \ + \r\n line continuation matching one-line template
    cmd_cont_crlf = "uv run deploy.py \\\r\n  --version 1.2"
    call_crlf = make_call(tool, "call_cont_crlf", project_id, args=BashArgs(command=cmd_cont_crlf))
    dec_crlf = await gate.decide([call_crlf])
    assert dec_crlf["call_cont_crlf"].outcome == "ask"

    # One-line command matching template with line continuation
    cmd_oneline = "uv run deploy_alt.py --version 1.2"
    call_tmpl_cont = make_call(
        tool, "call_tmpl_cont", project_id, args=BashArgs(command=cmd_oneline)
    )
    dec_tmpl_cont = await gate.decide([call_tmpl_cont])
    assert dec_tmpl_cont["call_tmpl_cont"].outcome == "ask"


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


@pytest.mark.asyncio
async def test_bash_matched_to_hold_hook_cannot_loosen_bash_own_decision(
    databases: Databases,
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    spec = _spec(
        kind="hold",
        template="uv run hold.py --model {model}",
        placeholders=["model"],
        deploys=True,
    )
    await service.register(project_id, spec, "hold_hook", "churn", "handoff")

    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)
    tool = BashTool(hooks=service)

    # At "ask" level: Bash normally asks. A hold hook with no handoff facts (deploys=None)
    # returns allow from decide_action. But the decision must remain "ask", never loosened to allow.
    await set_permission_level(databases, project_id, "ask")
    call_ask = make_call(
        tool, "call_hold_1", project_id, args=BashArgs(command="uv run hold.py --model churn")
    )
    decisions_ask = await gate.decide([call_ask])
    assert decisions_ask["call_hold_1"].outcome == "ask"

    # At "suggest" level: Bash normally denies. A hold hook with deploys=None returns ask from decide_action.
    # But the decision must remain "deny", never loosened to ask.
    await set_permission_level(databases, project_id, "suggest")
    call_sug = make_call(
        tool, "call_hold_2", project_id, args=BashArgs(command="uv run hold.py --model churn")
    )
    decisions_sug = await gate.decide([call_sug])
    assert decisions_sug["call_hold_2"].outcome == "deny"


@pytest.mark.asyncio
async def test_bash_hooks_lookup_error_asks_instead_of_crashing(
    databases: Databases,
) -> None:
    project_id = new_id("prj")
    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store)

    class BrokenHooks:
        async def list(self, project_id: str):
            raise RuntimeError("Database connection lost")

    tool = BashTool(hooks=BrokenHooks())  # type: ignore

    # At autonomous level: normally Bash is allow. On error, it fails closed to "ask" instead of crashing.
    await set_permission_level(databases, project_id, "autonomous")
    call_auto = make_call(tool, "call_err_1", project_id, args=BashArgs(command="ls -la"))
    decisions_auto = await gate.decide([call_auto])
    assert decisions_auto["call_err_1"].outcome == "ask"
    assert decisions_auto["call_err_1"].approval_id is not None

    # At suggest level: on error, it fails closed to "deny" instead of crashing.
    await set_permission_level(databases, project_id, "suggest")
    call_sug = make_call(tool, "call_err_2", project_id, args=BashArgs(command="ls -la"))
    decisions_sug = await gate.decide([call_sug])
    assert decisions_sug["call_err_2"].outcome == "deny"
