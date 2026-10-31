"""Integration tests for RuleAuthority, ApprovalGate, and HooksService (I23, I30, I33)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mlops_agents.agents.approvals.authority import (
    Authority,
    AuthorityRegistry,
    GateFacts,
)
from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.settings import set_permission_level
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.tools.base import ToolContext
from mlops_agents.agents.tools.run_hook import RunHookArgs, RunHookTool
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.ids import new_id
from mlops_agents.monitoring.expr import Compare
from mlops_agents.monitoring.hooks.contract import (
    HookCall,
    HookRefused,
    HookResult,
)
from mlops_agents.monitoring.hooks.service import HooksService
from mlops_agents.monitoring.results.contract import ResultRecord, source_id
from mlops_agents.monitoring.results.store import ResultsStore
from mlops_agents.monitoring.rules.authority import RuleAuthority
from mlops_agents.monitoring.rules.contract import (
    RuleLimits,
    RuleSpec,
    RuleTerm,
)
from mlops_agents.monitoring.rules.store import SqliteRules
from mlops_agents.proposals.payloads import CommandSpec
from mlops_agents.storage.databases import Databases
from tests.agents.gate_helpers import FakeFileAccess, make_call, setup_session_and_turn
from tests.conftest import memory_project_secrets
from tests.monitoring.hooks.conftest import FakeRunner
from tests.monitoring.rules.conftest import ResultsAdapter
from tests.proposals.samples import command
from tests.providers.fake_clock import FakeClock


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


def _tool_context(
    project_id: str,
    authority: Authority | None = None,
) -> ToolContext:
    return ToolContext(
        project_id=project_id,
        session_id="ses_1",
        turn_id="trn_1",
        agent="lead",
        task=None,
        tool_call_id="call_rb_1",
        files=FakeFileAccess(),
        authority=authority,
    )


def _make_rule_spec(
    hook_id: str,
    hook_kind: str = "rollback",
    model: str | None = "churn",
    per_incident: int = 1,
    per_day: int = 2,
    threshold: float = 0.5,
) -> RuleSpec:
    return RuleSpec(
        name="auto-rollback",
        why="Rollback on drift",
        model=model,
        terms=[
            RuleTerm(
                check_id="chk_drift",
                model=model,
                when=Compare(op="gt", field="value", value=threshold),
                window_minutes=60,
                quantifier="latest",
                min_records=1,
            )
        ],
        hook_id=hook_id,
        hook_kind=hook_kind,  # type: ignore[arg-type]
        limits=RuleLimits(per_incident=per_incident, per_day=per_day),
    )


def _make_record(
    check_id: str = "chk_drift",
    model: str | None = "churn",
    value: float = 0.8,
    ran_at: datetime | None = None,
) -> ResultRecord:
    return ResultRecord(
        check_id=check_id,
        origin="agent",
        ran_at=ran_at or datetime(2026, 10, 31, 6, 30, 0, tzinfo=UTC),
        fired=True,
        value=value,
        threshold=0.5,
        category="drift",
        subject="predictions",
        model=model,
        detail={},
        source_ref="checks/drift.py",
    )


def _cmd_spec(
    kind: str = "rollback",
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


@pytest.mark.asyncio
async def test_gate_rule_authority_at_ask_allows_and_records_rule_satisfied(
    databases: Databases,
) -> None:
    project_id = new_id("prj")
    clock = FakeClock(start=datetime(2026, 10, 31, 7, 0, 0, tzinfo=UTC))
    secrets = memory_project_secrets(databases)
    events = SqliteEventLog(databases)
    rules = SqliteRules(databases, clock=clock)
    results = ResultsAdapter(ResultsStore(databases))

    service = HooksService(
        databases=databases, secrets=secrets, events=events, clock=clock, rules=rules
    )
    runner = FakeRunner(result=HookResult(ok=True, detail="rollback executed"))
    service.add_runner(runner)

    hook = await service.register(project_id, _cmd_spec("rollback"), "rb_hook", "churn", "handoff")
    rule = await rules.store(
        project_id, _make_rule_spec(hook_id=hook.id, hook_kind="rollback"), "prp_1"
    )

    # Ingest satisfying result
    rec = _make_record(ran_at=clock.now() - timedelta(minutes=10), value=0.8)
    s_id = source_id("file", "*.jsonl", "data_conn")
    stored = await results.ingest(project_id, s_id, [("k1", rec)], seen_at=clock.now())
    rec_id = stored[0].id

    authorities = AuthorityRegistry()
    authorities.register(
        "rule", RuleAuthority(rules=rules, results=results, databases=databases, clock=clock)
    )
    authorities.set_facts(_StaticFacts(deploys=False))

    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "ask")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store, authorities=authorities)

    tool = RunHookTool(service)
    incident_id = "inc_gate_1"
    args = RunHookArgs(
        hook_id=hook.id,
        reason="rollback due to drift",
        incident_id=incident_id,
        rule_id=rule.id,
    )
    call = make_call(tool, "call_rb_1", project_id, args=args)

    # 1. At 'ask', citing holding rule runs without approval (outcome='allow')
    decisions = await gate.decide([call])
    dec = decisions["call_rb_1"]
    assert dec.outcome == "allow"
    assert dec.authority is not None
    assert dec.authority.basis == "rule"
    assert dec.authority.evidence == [rec_id]

    # 2. Run tool with the gate authority
    ctx = _tool_context(project_id, authority=dec.authority)
    output = await tool.run(ctx, args)
    assert output.is_error is False

    # 3. Timeline shows rule.satisfied event on the incident with result IDs
    sat_events = await events.history(project_id, types=["rule.satisfied"])
    assert len(sat_events) == 1
    assert sat_events[0].subject_id == incident_id
    assert sat_events[0].payload["rule_id"] == rule.id
    assert sat_events[0].payload["evidence"] == [rec_id]


@pytest.mark.asyncio
async def test_gate_rule_authority_at_suggest_is_denied(
    databases: Databases,
) -> None:
    project_id = new_id("prj")
    clock = FakeClock(start=datetime(2026, 10, 31, 7, 0, 0, tzinfo=UTC))
    secrets = memory_project_secrets(databases)
    events = SqliteEventLog(databases)
    rules = SqliteRules(databases, clock=clock)
    results = ResultsAdapter(ResultsStore(databases))

    service = HooksService(
        databases=databases, secrets=secrets, events=events, clock=clock, rules=rules
    )
    runner = FakeRunner(result=HookResult(ok=True, detail="rollback executed"))
    service.add_runner(runner)

    hook = await service.register(project_id, _cmd_spec("rollback"), "rb_hook", "churn", "handoff")
    rule = await rules.store(
        project_id, _make_rule_spec(hook_id=hook.id, hook_kind="rollback"), "prp_1"
    )

    rec = _make_record(ran_at=clock.now() - timedelta(minutes=10), value=0.8)
    s_id = source_id("file", "*.jsonl", "data_conn")
    await results.ingest(project_id, s_id, [("k1", rec)], seen_at=clock.now())

    authorities = AuthorityRegistry()
    authorities.register(
        "rule", RuleAuthority(rules=rules, results=results, databases=databases, clock=clock)
    )
    authorities.set_facts(_StaticFacts(deploys=False))

    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "suggest")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store, authorities=authorities)

    tool = RunHookTool(service)
    args = RunHookArgs(
        hook_id=hook.id,
        reason="rollback due to drift",
        incident_id="inc_suggest",
        rule_id=rule.id,
    )
    call = make_call(tool, "call_rb_sug", project_id, args=args)

    # At 'suggest', it is denied
    decisions = await gate.decide([call])
    dec = decisions["call_rb_sug"]
    assert dec.outcome == "deny"


@pytest.mark.asyncio
async def test_gate_failing_rule_condition_asks_at_ask(
    databases: Databases,
) -> None:
    project_id = new_id("prj")
    clock = FakeClock(start=datetime(2026, 10, 31, 7, 0, 0, tzinfo=UTC))
    secrets = memory_project_secrets(databases)
    events = SqliteEventLog(databases)
    rules = SqliteRules(databases, clock=clock)
    results = ResultsAdapter(ResultsStore(databases))

    service = HooksService(
        databases=databases, secrets=secrets, events=events, clock=clock, rules=rules
    )
    hook = await service.register(project_id, _cmd_spec("rollback"), "rb_hook", "churn", "handoff")
    rule = await rules.store(
        project_id, _make_rule_spec(hook_id=hook.id, hook_kind="rollback"), "prp_1"
    )

    # Ingest result with value 0.2 (does NOT satisfy condition value > 0.5)
    rec = _make_record(ran_at=clock.now() - timedelta(minutes=10), value=0.2)
    s_id = source_id("file", "*.jsonl", "data_conn")
    await results.ingest(project_id, s_id, [("k1", rec)], seen_at=clock.now())

    authorities = AuthorityRegistry()
    authorities.register(
        "rule", RuleAuthority(rules=rules, results=results, databases=databases, clock=clock)
    )
    authorities.set_facts(_StaticFacts(deploys=False))

    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "ask")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store, authorities=authorities)

    tool = RunHookTool(service)
    args = RunHookArgs(
        hook_id=hook.id,
        reason="rollback due to drift",
        incident_id="inc_ask_fail",
        rule_id=rule.id,
    )
    call = make_call(tool, "call_rb_fail", project_id, args=args)

    # Failing condition results in basis "none", so at 'ask' permission level it asks
    decisions = await gate.decide([call])
    dec = decisions["call_rb_fail"]
    assert dec.outcome == "ask"
    assert dec.approval_id is not None


@pytest.mark.asyncio
async def test_gate_deploy_hook_can_never_be_rule_cited(
    databases: Databases,
) -> None:
    project_id = new_id("prj")
    clock = FakeClock(start=datetime(2026, 10, 31, 7, 0, 0, tzinfo=UTC))
    secrets = memory_project_secrets(databases)
    events = SqliteEventLog(databases)
    rules = SqliteRules(databases, clock=clock)
    results = ResultsAdapter(ResultsStore(databases))

    service = HooksService(
        databases=databases, secrets=secrets, events=events, clock=clock, rules=rules
    )
    hook = await service.register(project_id, _cmd_spec("deploy"), "dep_hook", "churn", "handoff")

    authorities = AuthorityRegistry()
    authorities.register(
        "rule", RuleAuthority(rules=rules, results=results, databases=databases, clock=clock)
    )
    authorities.set_facts(_StaticFacts(deploys=True))

    await setup_session_and_turn(databases, project_id, "ses_1", "trn_1")
    await set_permission_level(databases, project_id, "ask")

    store = ApprovalStore(databases)
    gate = ApprovalGate(databases, store=store, authorities=authorities)

    tool = RunHookTool(service)
    # Even if an agent passes a rule_id for a deploy hook:
    args = RunHookArgs(
        hook_id=hook.id,
        reason="deploy model",
        incident_id="inc_dep",
        rule_id="rul_some_rule",
    )
    call = make_call(tool, "call_dep", project_id, args=args)

    decisions = await gate.decide([call])
    dec = decisions["call_dep"]
    # Action kind deploy takes no rule citation; at ask it always asks
    assert dec.outcome == "ask"
    assert dec.authority is None or dec.authority.basis == "none"


@pytest.mark.asyncio
async def test_concurrent_run_hook_calls_with_per_incident_1(
    databases: Databases,
) -> None:
    project_id = new_id("prj")
    clock = FakeClock(start=datetime(2026, 10, 31, 7, 0, 0, tzinfo=UTC))
    secrets = memory_project_secrets(databases)
    events = SqliteEventLog(databases)
    rules = SqliteRules(databases, clock=clock)

    service = HooksService(
        databases=databases, secrets=secrets, events=events, clock=clock, rules=rules
    )

    # Add a runner with a slight sleep so both calls execute concurrently
    class AsyncFakeRunner:
        type = "command"

        async def run(
            self, project_id: str, hook: Any, params: dict[str, str], authority: Authority
        ) -> HookResult:
            await asyncio.sleep(0.05)
            return HookResult(ok=True, detail="concurrent hook ok")

    service.add_runner(AsyncFakeRunner())  # type: ignore[arg-type]

    hook = await service.register(project_id, _cmd_spec("rollback"), "rb_hook", "churn", "handoff")
    rule = await rules.store(
        project_id,
        _make_rule_spec(hook_id=hook.id, hook_kind="rollback", per_incident=1),
        "prp_lim_conc",
    )

    auth = Authority(basis="rule", ref=rule.id, reason="Rule satisfied", evidence=["res_1"])
    call1 = HookCall(
        hook_id=hook.id,
        reason="run 1",
        incident_id="inc_conc",
        rule_id=rule.id,
    )
    call2 = HookCall(
        hook_id=hook.id,
        reason="run 2",
        incident_id="inc_conc",
        rule_id=rule.id,
    )

    # Run two calls concurrently with the same rule and incident
    res1, res2 = await asyncio.gather(
        service.run(project_id, call1, auth),
        service.run(project_id, call2, auth),
        return_exceptions=True,
    )

    results_list = [res1, res2]
    successes = [r for r in results_list if isinstance(r, HookResult) and r.ok]
    refusals = [r for r in results_list if isinstance(r, HookRefused)]

    assert len(successes) == 1
    assert len(refusals) == 1
    assert "per_incident limit" in str(refusals[0])
