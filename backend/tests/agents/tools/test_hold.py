"""Tests for HoldTool (I33)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mlops_agents.agents.approvals.authority import (
    ActionCall,
    Authority,
    AuthorityRegistry,
    GateFacts,
)
from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.settings import set_permission_level
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.tools.base import RiskClass, ToolContext, ToolFailure
from mlops_agents.agents.tools.hold import HoldArgs, HoldTool
from mlops_agents.agents.tools.names import HOLD
from mlops_agents.credentials.project_secrets import ProjectSecrets
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.ids import new_id
from mlops_agents.monitoring.expectations.contract import NeverSuspended
from mlops_agents.monitoring.hooks.contract import HookResult
from mlops_agents.monitoring.hooks.service import HooksService
from mlops_agents.monitoring.incidents.contract import (
    HoldScope,
    MediumSeverity,
    SignalDraft,
)
from mlops_agents.monitoring.incidents.service import IncidentsService
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.monitoring.untrusted import untrusted
from mlops_agents.proposals.handoffs import SqliteHandoffs
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
def handoffs(databases: Databases) -> SqliteHandoffs:
    return SqliteHandoffs(databases)


@pytest.fixture
def service(
    databases: Databases,
    secrets: ProjectSecrets,
    events: SqliteEventLog,
    clock: FakeClock,
) -> HooksService:
    return HooksService(databases=databases, secrets=secrets, events=events, clock=clock)


class _StaticFacts(GateFacts):
    def __init__(self, deploys: bool | None = False) -> None:
        self._deploys = deploys

    async def facts(
        self, project_id: str, hook_id: str | None
    ) -> tuple[bool | None, bool | None, bool | None]:
        return (self._deploys, True, True)


def _spec(
    kind: str = "hold",
    template: str | None = None,
    placeholders: list[str] | None = None,
    deploys: bool = False,
) -> CommandSpec:
    tmpl = template or "python hold.py"
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
        tool_call_id="call_hold_1",
        authority=authority,
    )


@pytest.fixture
def incidents_service(
    databases: Databases,
    events: SqliteEventLog,
    handoffs: SqliteHandoffs,
    clock: FakeClock,
) -> IncidentsService:
    results = ResultsService(
        databases=databases,
        handoffs=handoffs,
        connections=None,  # pyright: ignore[reportArgumentType]
        events=events,
    )
    return IncidentsService(
        databases=databases,
        events=events,
        results=results,
        severity=MediumSeverity(),
        maintenance=NeverSuspended(),
        clock=clock,
    )


async def _create_incident(
    incidents: IncidentsService,
    project_id: str,
    model: str | None = "churn",
) -> str:
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    draft = SignalDraft(
        kind="fired_result",
        category="data_quality",
        subject="predictions_table",
        model=model,
        ran_at=now,
        detail={"error": "nulls in column score"},
    )
    outcome = await incidents.raise_signal(project_id, draft, seen_at=now)
    return outcome.incident_id


@pytest.mark.asyncio
async def test_hold_tool_attributes(
    service: HooksService,
    incidents_service: IncidentsService,
) -> None:
    tool = HoldTool(service, incidents_service)
    assert tool.name == HOLD
    assert tool.name == "Hold"
    assert tool.risk == RiskClass.DEPLOY
    assert tool.description


@pytest.mark.asyncio
async def test_action_call_resolves_hook_and_model(
    service: HooksService,
    incidents_service: IncidentsService,
) -> None:
    project_id = new_id("prj")
    inc_id = await _create_incident(incidents_service, project_id, model="churn")

    spec = _spec(kind="hold")
    hook = await service.register(project_id, spec, "churn_hold", "churn", "handoff")

    tool = HoldTool(service, incidents_service)
    ctx = _tool_context(project_id, agent="test_agent")
    args = HoldArgs(
        incident_id=inc_id,
        scope=HoldScope(tables=["predictions_table"]),
        reason="Holding bad data",
    )

    call = await tool.action_call(args, ctx)
    assert isinstance(call, ActionCall)
    assert call.project_id == project_id
    assert call.agent == "test_agent"
    assert call.kind == "hold"
    assert call.hook_id == hook.id
    assert call.model == "churn"
    assert call.incident_id == inc_id
    assert call.deploys is None
    assert call.has_production_signal is None
    assert call.team_gate_authority is None


@pytest.mark.asyncio
async def test_action_call_unknown_incident_raises(
    service: HooksService,
    incidents_service: IncidentsService,
) -> None:
    project_id = new_id("prj")
    tool = HoldTool(service, incidents_service)
    ctx = _tool_context(project_id)
    args = HoldArgs(
        incident_id="inc_unknown",
        scope=HoldScope(tables=["tbl"]),
        reason="reason",
    )
    with pytest.raises(ToolFailure, match="inc_unknown"):
        await tool.action_call(args, ctx)


@pytest.mark.asyncio
async def test_run_without_authority_raises(
    service: HooksService,
    incidents_service: IncidentsService,
) -> None:
    project_id = new_id("prj")
    inc_id = await _create_incident(incidents_service, project_id, model="churn")

    tool = HoldTool(service, incidents_service)
    ctx = _tool_context(project_id, authority=None)
    args = HoldArgs(
        incident_id=inc_id,
        scope=HoldScope(tables=["predictions_table"]),
        reason="holding data",
    )
    with pytest.raises(RuntimeError, match="Hold reached without authority"):
        await tool.run(ctx, args)


@pytest.mark.asyncio
async def test_hold_with_model_hold_hook_records_hold_and_runs_hook(
    service: HooksService,
    incidents_service: IncidentsService,
) -> None:
    project_id = new_id("prj")
    inc_id = await _create_incident(incidents_service, project_id, model="churn")

    runner = FakeRunner(result=HookResult(ok=True, detail="hold command executed"))
    service.add_runner(runner)

    spec = _spec(kind="hold")
    hook = await service.register(project_id, spec, "churn_hold", "churn", "handoff")

    tool = HoldTool(service, incidents_service)
    auth = Authority(basis="approval", reason="authorized")
    ctx = _tool_context(project_id, authority=auth)
    args = HoldArgs(
        incident_id=inc_id,
        scope=HoldScope(tables=["predictions_table"]),
        reason="Holding data for churn model",
    )

    output = await tool.run(ctx, args)
    assert output.is_error is False
    assert inc_id in output.content
    assert untrusted("hold command executed", source="hook_run") in output.content

    # 1. Incident is held
    inc = await incidents_service.get(project_id, inc_id)
    assert inc is not None
    assert inc.held is True

    # 2. Hook ran once with the incident ID
    assert len(runner.calls) == 1
    call_proj, call_hook, _call_params, call_auth = runner.calls[0]
    assert call_proj == project_id
    assert call_hook.id == hook.id
    assert call_auth == auth


@pytest.mark.asyncio
async def test_hold_with_no_hold_hook(
    service: HooksService,
    incidents_service: IncidentsService,
) -> None:
    project_id = new_id("prj")
    inc_id = await _create_incident(incidents_service, project_id, model="churn")

    runner = FakeRunner()
    service.add_runner(runner)

    tool = HoldTool(service, incidents_service)
    auth = Authority(basis="approval", reason="authorized")
    ctx = _tool_context(project_id, authority=auth)
    args = HoldArgs(
        incident_id=inc_id,
        scope=HoldScope(tables=["predictions_table"]),
        reason="No hook hold",
    )

    output = await tool.run(ctx, args)
    assert output.is_error is False
    assert "No hold command is registered" in output.content
    assert inc_id in output.content

    # Incident is held, no runner call
    inc = await incidents_service.get(project_id, inc_id)
    assert inc is not None
    assert inc.held is True
    assert len(runner.calls) == 0


@pytest.mark.asyncio
async def test_hold_with_two_active_hold_hooks(
    service: HooksService,
    incidents_service: IncidentsService,
) -> None:
    project_id = new_id("prj")
    inc_id = await _create_incident(incidents_service, project_id, model="churn")

    runner = FakeRunner()
    service.add_runner(runner)

    spec1 = _spec(kind="hold")
    hook1 = await service.register(project_id, spec1, "hold_1", "churn", "handoff")

    spec2 = _spec(kind="hold")
    hook2 = await service.register(project_id, spec2, "hold_2", "churn", "handoff")

    tool = HoldTool(service, incidents_service)
    auth = Authority(basis="approval", reason="authorized")
    ctx = _tool_context(project_id, authority=auth)
    args = HoldArgs(
        incident_id=inc_id,
        scope=HoldScope(tables=["predictions_table"]),
        reason="Two hooks hold",
    )

    output = await tool.run(ctx, args)
    assert output.is_error is False
    assert "Several active hold hooks found" in output.content
    assert hook1.id in output.content
    assert hook2.id in output.content
    assert "RunHook" in output.content

    # Incident is held, none run
    inc = await incidents_service.get(project_id, inc_id)
    assert inc is not None
    assert inc.held is True
    assert len(runner.calls) == 0


@pytest.mark.asyncio
async def test_hold_with_failing_hook_leaves_incident_held(
    service: HooksService,
    incidents_service: IncidentsService,
) -> None:
    project_id = new_id("prj")
    inc_id = await _create_incident(incidents_service, project_id, model="churn")

    runner = FakeRunner(result=HookResult(ok=False, detail="failed to connect to data store"))
    service.add_runner(runner)

    spec = _spec(kind="hold")
    hook = await service.register(project_id, spec, "churn_hold", "churn", "handoff")

    tool = HoldTool(service, incidents_service)
    auth = Authority(basis="approval", reason="authorized")
    ctx = _tool_context(project_id, authority=auth)
    args = HoldArgs(
        incident_id=inc_id,
        scope=HoldScope(tables=["predictions_table"]),
        reason="Failing hook hold",
    )

    output = await tool.run(ctx, args)
    assert output.is_error is True
    assert hook.id in output.content
    assert untrusted("failed to connect to data store", source="hook_run") in output.content

    # Incident remains held!
    inc = await incidents_service.get(project_id, inc_id)
    assert inc is not None
    assert inc.held is True


@pytest.mark.asyncio
async def test_real_gate_hold_asks_at_suggest_and_runs_at_ask(
    databases: Databases,
    service: HooksService,
    incidents_service: IncidentsService,
) -> None:
    project_id = new_id("prj")
    inc_id = await _create_incident(incidents_service, project_id, model="churn")

    runner = FakeRunner(result=HookResult(ok=True, detail="hold executed"))
    service.add_runner(runner)

    spec = _spec(kind="hold")
    await service.register(project_id, spec, "churn_hold", "churn", "handoff")

    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")

    authorities = AuthorityRegistry()
    authorities.set_facts(_StaticFacts(deploys=False))
    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store, authorities=authorities)

    tool = HoldTool(service, incidents_service)
    args = HoldArgs(
        incident_id=inc_id,
        scope=HoldScope(tables=["predictions_table"]),
        reason="test gate",
    )
    call = make_call(tool, "call_hold_1", project_id, args=args)

    # 1. At "suggest": asks
    await set_permission_level(databases, project_id, "suggest")
    dec_sug = await gate.decide([call])
    assert dec_sug["call_hold_1"].outcome == "ask"

    # 2. At "ask": runs (allowed)
    await set_permission_level(databases, project_id, "ask")
    dec_ask = await gate.decide([call])
    assert dec_ask["call_hold_1"].outcome == "allow"
    assert dec_ask["call_hold_1"].authority is not None

    ctx = _tool_context(project_id, authority=dec_ask["call_hold_1"].authority)
    output = await tool.run(ctx, args)
    assert output.is_error is False
    assert len(runner.calls) == 1


def test_default_registry_hold(
    service: HooksService,
    incidents_service: IncidentsService,
) -> None:
    from mlops_agents.agents.tools import default_registry

    reg_without = default_registry()
    assert "Hold" not in reg_without.names()

    reg_hooks_only = default_registry(hooks=service)
    assert "Hold" not in reg_hooks_only.names()

    reg_incidents_only = default_registry(incidents=incidents_service)
    assert "Hold" not in reg_incidents_only.names()

    reg_both = default_registry(hooks=service, incidents=incidents_service)
    assert "Hold" in reg_both.names()
    assert isinstance(reg_both.get("Hold"), HoldTool)


@pytest.mark.asyncio
async def test_hold_skips_suspended_and_disabled_hold_hook(
    service: HooksService,
    incidents_service: IncidentsService,
) -> None:
    project_id = "prj_holdstat1"
    runner = FakeRunner()
    service.add_runner(runner)

    spec = _spec(kind="hold")
    hook = await service.register(
        project_id,
        spec=spec,
        name="hold-cmd",
        model="iris",
        source="handoff",
    )
    # Suspend the hook
    await service.suspend(project_id, hook.id, "unapproved changes")

    inc_id = await _create_incident(incidents_service, project_id, model="iris")
    tool = HoldTool(hooks=service, incidents=incidents_service)
    ctx = _tool_context(project_id, authority=Authority(basis="approval", reason="approved"))
    args = HoldArgs(
        incident_id=inc_id,
        scope=HoldScope(tables=["features"]),
        reason="data corrupt",
    )

    # 1. Action call should have hook_id=None because the hook is suspended
    action = await tool.action_call(args, ctx)
    assert action is not None
    assert action.hook_id is None

    # 2. Run should record hold, skip running the hook, and clearly message that it is suspended
    output = await tool.run(ctx, args)
    assert output.is_error is False
    assert len(runner.calls) == 0
    assert "is suspended (unapproved changes)" in output.content

    held_inc = await incidents_service.get(project_id, inc_id)
    assert held_inc is not None
    assert held_inc.held is True

    # 3. Disable the hook: run should still skip running the hook
    await service.set_enabled(project_id, hook.id, False)
    inc_id2 = await _create_incident(incidents_service, project_id, model="iris")
    args2 = HoldArgs(
        incident_id=inc_id2,
        scope=HoldScope(tables=["features"]),
        reason="data corrupt again",
    )
    action2 = await tool.action_call(args2, ctx)
    assert action2 is not None
    assert action2.hook_id is None

    output2 = await tool.run(ctx, args2)
    assert output2.is_error is False
    assert len(runner.calls) == 0
    assert "No hold command is registered" in output2.content
