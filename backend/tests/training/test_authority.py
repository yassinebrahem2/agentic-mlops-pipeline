"""Tests for PolicyAuthority, policy_limit guard, and policy window/deadline (Task 8, #493)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mlops_agents.agents.approvals.authority import (
    ActionCall,
)
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.hooks.contract import (
    HookCall,
    HookResult,
    HookView,
)
from mlops_agents.monitoring.hooks.placeholders import TrackerPlaceholderResolver
from mlops_agents.monitoring.rules.contract import RuleLimits, RuleSpec, RuleView
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.payloads import CommandSpec
from mlops_agents.storage.databases import Databases
from mlops_agents.tracking.contract import (
    CiRunRow,
    ModelSummary,
    Page,
    TrackingQuery,
)
from mlops_agents.training.authority import PolicyAuthority
from mlops_agents.training.contract import (
    GuardResult,
    TrainingPolicyPayload,
    WindowRule,
)
from mlops_agents.training.guards import check_policy_limit
from mlops_agents.training.lifecycle import TrainingLifecycle, resolve_window
from mlops_agents.training.requests import SqliteTraining
from tests.providers.fake_clock import FakeClock


class FakeHooks:
    def __init__(self, hooks: list[HookView] | None = None) -> None:
        self._hooks = {h.id: h for h in (hooks or [])}

    async def get(self, project_id: str, hook_id: str) -> HookView | None:
        return self._hooks.get(hook_id)

    async def list(self, project_id: str, model: str | None = None) -> list[HookView]:
        if model is None:
            return list(self._hooks.values())
        return [h for h in self._hooks.values() if h.model == model]

    async def suspend(self, project_id: str, hook_id: str, reason: str) -> HookView:
        if hook_id not in self._hooks:
            raise KeyError(f"Hook {hook_id} not found")
        h = self._hooks[hook_id]
        updated = h.model_copy(update={"status": "suspended", "status_detail": reason})
        self._hooks[hook_id] = updated
        return updated


class FakeRules:
    def __init__(self, rules: list[RuleView] | None = None) -> None:
        self._rules = list(rules) if rules else []

    async def list(self, project_id: str, model: str | None = None) -> list[RuleView]:
        if model is None:
            return list(self._rules)
        return [r for r in self._rules if r.spec.model == model or r.spec.model is None]

    async def store(self, project_id: str, spec: RuleSpec, proposal_id: str) -> RuleView:
        rule = RuleView(
            id=f"rul_{len(self._rules) + 1}",
            spec=spec,
            enabled=True,
            proposal_id=proposal_id,
            approved=datetime.now(UTC),
        )
        self._rules.append(rule)
        return rule

    async def set_enabled(self, project_id: str, rule_id: str, enabled: bool) -> RuleView:
        for r in self._rules:
            if r.id == rule_id:
                updated = r.model_copy(update={"enabled": enabled})
                self._rules.remove(r)
                self._rules.append(updated)
                return updated
        raise KeyError(f"Rule {rule_id} not found")


class FakeTracking:
    def __init__(
        self,
        ci_runs: list[CiRunRow] | None = None,
        models: list[ModelSummary] | None = None,
    ) -> None:
        self._ci_runs = list(ci_runs) if ci_runs else []
        self._models = list(models) if models else []

    def set_ci_runs(self, runs: list[CiRunRow]) -> None:
        self._ci_runs = runs

    async def query(self, project_id: str, q: TrackingQuery) -> Page[Any]:
        if q.what == "ci_runs":
            rows = [r for r in self._ci_runs]
            return Page(rows=rows, total=len(rows), truncated=False)
        if q.what == "models":
            return Page(rows=self._models, total=len(self._models), truncated=False)
        return Page(rows=[], total=0, truncated=False)


class FakeExpectations:
    def __init__(self) -> None:
        self.registered: list[Any] = []

    async def register(self, project_id: str, spec: Any) -> None:
        self.registered.append(spec)

    async def retire(self, project_id: str, key: Any) -> None:
        pass


class FakeFixes:
    def __init__(self, policy_authority: Any = None) -> None:
        self.policy_authority = policy_authority

    def bind_policy_authority(self, authority: Any) -> None:
        self.policy_authority = authority

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def guards(self, *args: Any, **kwargs: Any) -> GuardResult:
        return GuardResult(ok=True, refusals=[])

    async def check_guards(self, *args: Any, **kwargs: Any) -> GuardResult:
        return GuardResult(ok=True, refusals=[])


def _make_hook(
    hook_id: str,
    kind: str = "train",
    hook_type: str = "ci_dispatch",
    workflow: str | None = ".github/workflows/train.yml",
    deploys: bool = False,
) -> HookView:
    spec = CommandSpec(
        kind=kind,  # type: ignore[arg-type]
        type=hook_type,  # type: ignore[arg-type]
        workflow=workflow if hook_type == "ci_dispatch" else None,
        template="python run.py" if hook_type == "command" else None,
        inputs={},
        placeholders=[],
        entry_files=["train.py"],
        deploys=deploys,
    )
    return HookView(
        id=hook_id,
        name=hook_id,
        model="churn_risk",
        spec=spec,
        pins=[],
        credential_set=False,
        status="active",
        suspended_reason=None,
        approved_commit="abc1234",
        deploys_verified=None,
        source="proposal",
        created=datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC),
    )


def _make_payload(
    model: str = "churn_risk",
    train_hook: str = "hok_train",
    eval_hook: str | None = "hok_eval",
    min_interval_hours: int = 24,
    max_runs_per_week: int = 3,
    run_time_limit_minutes: int = 120,
) -> TrainingPolicyPayload:
    return TrainingPolicyPayload(
        model=model,
        train_hook_id=train_hook,
        evaluate_hook_id=eval_hook,
        window=WindowRule(kind="trailing_days", days=30),
        min_interval_hours=min_interval_hours,
        max_runs_per_week=max_runs_per_week,
        run_time_limit_minutes=run_time_limit_minutes,
    )


@pytest.mark.asyncio
async def test_policy_authority_within_limits(tmp_path: Path) -> None:
    databases = Databases(DataPaths(tmp_path / "data"))
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)
    events = SqliteEventLog(databases)
    training = SqliteTraining(databases, events, clock)

    pol = await training.policies_store.store(
        "prj_1", _make_payload(train_hook="hok_train"), proposal_id="prop_1"
    )

    hook = _make_hook("hok_train", deploys=False)
    hooks: Any = FakeHooks([hook])

    # Past runs: 30 hours ago (satisfied min_interval_hours=24)
    past_run = CiRunRow(
        source_id="ci_1",
        key="run_1",
        workflow=".github/workflows/train.yml",
        status="succeeded",
        start=now - timedelta(hours=30),
        duration_s=600.0,
        commit="commit1",
        url="https://ci/1",
    )
    tracking: Any = FakeTracking(ci_runs=[past_run])

    authority = PolicyAuthority(
        policies=training.policies_store,
        training=training,
        tracking=tracking,
        hooks=hooks,
        clock=clock,
    )

    call = ActionCall(
        project_id="prj_1",
        agent="retraining",
        kind="train",
        model="churn_risk",
        hook_id="hok_train",
        policy_id=pol.id,
    )
    auth = await authority.authorize(call)
    assert auth.basis == "policy"
    assert auth.ref == pol.id
    assert "satisfied" in auth.reason


@pytest.mark.asyncio
async def test_policy_authority_rejections(tmp_path: Path) -> None:
    databases = Databases(DataPaths(tmp_path / "data"))
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)
    events = SqliteEventLog(databases)
    training = SqliteTraining(databases, events, clock)

    pol = await training.policies_store.store(
        "prj_1",
        _make_payload(train_hook="hok_train", min_interval_hours=24, max_runs_per_week=2),
        proposal_id="prop_1",
    )

    hook = _make_hook("hok_train", deploys=False)
    hooks: Any = FakeHooks([hook])
    tracking: Any = FakeTracking()

    authority = PolicyAuthority(
        policies=training.policies_store,
        training=training,
        tracking=tracking,
        hooks=hooks,
        clock=clock,
    )

    # 1. No policy cited
    c1 = ActionCall(project_id="prj_1", agent="retraining", kind="train", policy_id=None)
    assert (await authority.authorize(c1)).basis == "none"

    # 2. Unknown policy
    c2 = ActionCall(project_id="prj_1", agent="retraining", kind="train", policy_id="pol_unknown")
    assert (await authority.authorize(c2)).basis == "none"

    # 3. Model mismatch
    c3 = ActionCall(
        project_id="prj_1",
        agent="retraining",
        kind="train",
        model="other_model",
        policy_id=pol.id,
    )
    assert (await authority.authorize(c3)).basis == "none"

    # 4. Hook mismatch
    c4 = ActionCall(
        project_id="prj_1",
        agent="retraining",
        kind="train",
        hook_id="hok_other",
        policy_id=pol.id,
    )
    assert (await authority.authorize(c4)).basis == "none"

    # 5. Suspended policy
    await training.policies_store.set_status("prj_1", pol.id, "suspended", reason="testing")
    c5 = ActionCall(project_id="prj_1", agent="retraining", kind="train", policy_id=pol.id)
    auth5 = await authority.authorize(c5)
    assert auth5.basis == "none"
    assert "suspended" in auth5.reason

    # Re-activate policy for rate limit checks
    await training.policies_store.set_status("prj_1", pol.id, "active")

    # 6. min_interval_hours violated
    recent_run = CiRunRow(
        source_id="ci_1",
        key="run_recent",
        workflow=".github/workflows/train.yml",
        status="succeeded",
        start=now - timedelta(hours=5),  # 5h ago < 24h
        duration_s=600.0,
        commit="commit1",
        url="https://ci/recent",
    )
    tracking.set_ci_runs([recent_run])
    c6 = ActionCall(project_id="prj_1", agent="retraining", kind="train", policy_id=pol.id)
    auth6 = await authority.authorize(c6)
    assert auth6.basis == "none"
    assert "min_interval_hours" in auth6.reason

    # 7. max_runs_per_week exceeded (team's own runs count!)
    runs_7d = [
        CiRunRow(
            source_id="ci_1",
            key=f"run_{i}",
            workflow=".github/workflows/train.yml",
            status="succeeded",
            start=now - timedelta(days=i + 1),  # all > 24h apart, but 2 runs already
            duration_s=600.0,
            commit=f"c{i}",
            url=f"https://ci/{i}",
        )
        for i in range(2)
    ]
    tracking.set_ci_runs(runs_7d)
    c7 = ActionCall(project_id="prj_1", agent="retraining", kind="train", policy_id=pol.id)
    auth7 = await authority.authorize(c7)
    assert auth7.basis == "none"
    assert "max_runs_per_week" in auth7.reason


@pytest.mark.asyncio
async def test_policy_authority_command_hook_counts_trq(tmp_path: Path) -> None:
    databases = Databases(DataPaths(tmp_path / "data"))
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)
    events = SqliteEventLog(databases)
    training = SqliteTraining(databases, events, clock)

    pol = await training.policies_store.store(
        "prj_1",
        _make_payload(train_hook="hok_cmd", min_interval_hours=24, max_runs_per_week=2),
        proposal_id="prop_1",
    )

    cmd_hook = _make_hook("hok_cmd", hook_type="command", workflow=None, deploys=False)
    hooks: Any = FakeHooks([cmd_hook])
    tracking: Any = FakeTracking()  # Empty CI runs

    authority = PolicyAuthority(
        policies=training.policies_store,
        training=training,
        tracking=tracking,
        hooks=hooks,
        clock=clock,
    )

    # Within limits
    c = ActionCall(
        project_id="prj_1",
        agent="retraining",
        kind="train",
        hook_id="hok_cmd",
        policy_id=pol.id,
    )
    assert (await authority.authorize(c)).basis == "policy"

    # Insert a recent trq row (5 hours ago)
    clock.advance(-5 * 3600)
    await training.create_request("prj_1", "trq_recent", "churn_risk", "hok_cmd", "train")
    clock.advance(5 * 3600)

    auth = await authority.authorize(c)
    assert auth.basis == "none"
    assert "min_interval_hours" in auth.reason


@pytest.mark.asyncio
async def test_policy_authority_deploying_hook_requires_rollback_rule(tmp_path: Path) -> None:
    databases = Databases(DataPaths(tmp_path / "data"))
    clock = FakeClock()
    events = SqliteEventLog(databases)
    training = SqliteTraining(databases, events, clock)

    pol = await training.policies_store.store(
        "prj_1", _make_payload(train_hook="hok_deploy"), proposal_id="prop_1"
    )

    deploy_hook = _make_hook("hok_deploy", deploys=True)
    hooks: Any = FakeHooks([deploy_hook])
    tracking: Any = FakeTracking()
    rules: Any = FakeRules([])

    authority = PolicyAuthority(
        policies=training.policies_store,
        training=training,
        tracking=tracking,
        hooks=hooks,
        clock=clock,
        rules=rules,
    )

    c = ActionCall(
        project_id="prj_1",
        agent="retraining",
        kind="train",
        hook_id="hok_deploy",
        policy_id=pol.id,
    )
    auth = await authority.authorize(c)
    assert auth.basis == "none"
    assert "rollback rule" in auth.reason

    # Add enabled rollback rule
    await rules.store(
        "prj_1",
        RuleSpec(
            name="rb",
            why="w",
            model="churn_risk",
            terms=[],
            hook_id="hok_rb",
            hook_kind="rollback",
            limits=RuleLimits(),
        ),
        proposal_id="pr_rule",
    )

    auth2 = await authority.authorize(c)
    assert auth2.basis == "policy"


@pytest.mark.asyncio
async def test_policy_limit_guard_integration(tmp_path: Path) -> None:
    databases = Databases(DataPaths(tmp_path / "data"))
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)
    events = SqliteEventLog(databases)
    training = SqliteTraining(databases, events, clock)

    pol = await training.policies_store.store(
        "prj_1", _make_payload(min_interval_hours=24), proposal_id="prop_1"
    )

    hook = _make_hook("hok_train", deploys=False)
    hooks: Any = FakeHooks([hook])

    # Recent run violates min_interval_hours
    recent_run = CiRunRow(
        source_id="ci_1",
        key="run_recent",
        workflow=".github/workflows/train.yml",
        status="succeeded",
        start=now - timedelta(hours=2),
        duration_s=600.0,
        commit="commit1",
        url="https://ci/recent",
    )
    tracking: Any = FakeTracking(ci_runs=[recent_run])

    authority = PolicyAuthority(
        policies=training.policies_store,
        training=training,
        tracking=tracking,
        hooks=hooks,
        clock=clock,
    )

    refusal = await check_policy_limit(
        authority,
        project_id="prj_1",
        model="churn_risk",
        policy_id=pol.id,
        hook_id="hok_train",
    )
    assert refusal is not None
    assert refusal.code == "policy_limit"
    assert "min_interval_hours" in refusal.detail


def test_resolve_window() -> None:
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)

    # 1. trailing_days
    r1 = WindowRule(kind="trailing_days", days=14)
    w1 = resolve_window(r1, now)
    assert w1.end == now
    assert w1.start == now - timedelta(days=14)

    # 2. since_last_run
    last_end = datetime(2026, 10, 20, 0, 0, 0, tzinfo=UTC)
    r2 = WindowRule(kind="since_last_run")
    w2 = resolve_window(r2, now, last_linked_end=last_end)
    assert w2.end == now
    assert w2.start == last_end

    # since_last_run with no previous run falls back to now
    w2_none = resolve_window(r2, now, last_linked_end=None)
    assert w2_none.start == now
    assert w2_none.end == now

    # 3. fixed
    fixed_start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    fixed_end = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
    r3 = WindowRule(kind="fixed", start=fixed_start, end=fixed_end)
    w3 = resolve_window(r3, now)
    assert w3.start == fixed_start
    assert w3.end == fixed_end


@pytest.mark.asyncio
async def test_lifecycle_uses_policy_window_and_deadline(tmp_path: Path) -> None:
    databases = Databases(DataPaths(tmp_path / "data"))
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)
    events = SqliteEventLog(databases)
    training = SqliteTraining(databases, events, clock)

    pol = await training.policies_store.store(
        "prj_1",
        _make_payload(min_interval_hours=24, run_time_limit_minutes=90),
        proposal_id="prop_1",
    )

    hook = _make_hook("hok_train", deploys=False)
    hooks: Any = FakeHooks([hook])
    tracking: Any = FakeTracking()
    authority = PolicyAuthority(
        policies=training.policies_store,
        training=training,
        tracking=tracking,
        hooks=hooks,
        clock=clock,
    )

    fixes = FakeFixes(policy_authority=authority)
    expectations: Any = FakeExpectations()
    resolver = TrackerPlaceholderResolver(tracking)

    lifecycle = TrainingLifecycle(
        training=training,
        fixes=fixes,  # type: ignore[arg-type]
        resolver=resolver,
        clock=clock,
        events=events,
        expectations=expectations,  # type: ignore[arg-type]
    )

    # 1. before() without window_start/window_end, but citing policy_id
    call = HookCall(
        hook_id="hok_train",
        reason="automated training",
        params={"candidate_version": "v1.0.0"},
        fix_id=None,
        policy_id=pol.id,
    )
    updated_call = await lifecycle.before("prj_1", hook, call)
    assert "request_id" in updated_call.params
    req_id = updated_call.params["request_id"]

    # Verify request was created with resolved window from policy (trailing 30 days)
    req = await training.request("prj_1", req_id)
    assert req is not None
    assert req.window is not None
    assert req.window.end == now
    assert req.window.start == now - timedelta(days=30)

    # 2. after() derives due_at from policy.run_time_limit_minutes (90 min)
    result = HookResult(ok=True, detail="ok", request_id=req_id)
    await lifecycle.after("prj_1", hook, updated_call, result)

    assert len(expectations.registered) == 1
    exp = expectations.registered[0]
    expected_due = now + timedelta(minutes=90)
    assert exp.due_at == expected_due
