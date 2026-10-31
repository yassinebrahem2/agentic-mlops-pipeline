"""Tests for SqliteTrainingPolicies, TrainingPolicyKind, and policies routes (Task 8, #493)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.hooks.contract import (
    HookView,
)
from mlops_agents.monitoring.query import MonitoringQuery
from mlops_agents.monitoring.rules.contract import RuleLimits, RuleSpec, RuleView
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.payloads import CommandSpec
from mlops_agents.storage.databases import Databases
from mlops_agents.training.contract import (
    TrainingPolicyPayload,
    WindowRule,
)
from mlops_agents.training.policies import (
    SqliteTrainingPolicies,
    TrainingPolicyKind,
)
from mlops_agents.training.queries import PoliciesQueryProvider
from mlops_agents.training.requests import SqliteTraining
from tests.api.conftest import LOCAL_URL, add_local_project, signed_in, started_app
from tests.providers.fake_clock import FakeClock


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    async with started_app(tmp_path / "data") as application:
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    async with signed_in(app, LOCAL_URL) as http:
        yield http


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


def _make_hook(
    hook_id: str,
    kind: str = "train",
    hook_type: str = "ci_dispatch",
    workflow: str | None = ".github/workflows/train.yml",
    deploys: bool = False,
    status: str = "active",
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
        status=status,  # type: ignore[arg-type]
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
    deploys: bool = False,
) -> TrainingPolicyPayload:
    return TrainingPolicyPayload(
        model=model,
        train_hook_id=train_hook,
        evaluate_hook_id=eval_hook,
        window=WindowRule(kind="trailing_days", days=30),
        min_interval_hours=24,
        max_runs_per_week=3,
        run_time_limit_minutes=120,
    )


@pytest.mark.asyncio
async def test_sqlite_training_policies_crud(tmp_path: Path) -> None:
    databases = Databases(DataPaths(tmp_path / "data"))
    clock = FakeClock(datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC))
    events = SqliteEventLog(databases)
    store = SqliteTrainingPolicies(databases, clock, events)

    payload = _make_payload()
    pol = await store.store("prj_1", payload, proposal_id="prop_1")

    assert pol.id.startswith("pol_")
    assert pol.status == "active"
    assert pol.proposal_id == "prop_1"
    assert pol.payload.model == "churn_risk"

    # Get
    fetched = await store.get("prj_1", pol.id)
    assert fetched is not None
    assert fetched.id == pol.id
    assert fetched.payload.model == "churn_risk"

    # Find by proposal
    by_prop = await store.find_by_proposal("prj_1", "prop_1")
    assert by_prop is not None
    assert by_prop.id == pol.id

    # List
    all_pols = await store.list("prj_1")
    assert len(all_pols) == 1
    assert all_pols[0].id == pol.id

    filtered = await store.list("prj_1", model="other_model")
    assert len(filtered) == 0

    # Set status
    updated = await store.set_status("prj_1", pol.id, "disabled", reason="test")
    assert updated.status == "disabled"
    assert updated.suspended_reason == "test"

    # Disable active for model
    pol2 = await store.store("prj_1", payload, proposal_id="prop_2")
    assert pol2.status == "active"
    await store.disable_active_for_model("prj_1", "churn_risk")
    after_disable = await store.get("prj_1", pol2.id)
    assert after_disable is not None
    assert after_disable.status == "disabled"


@pytest.mark.asyncio
async def test_suspend_and_activate_for_hook(tmp_path: Path) -> None:
    databases = Databases(DataPaths(tmp_path / "data"))
    clock = FakeClock(datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC))
    events = SqliteEventLog(databases)
    store = SqliteTrainingPolicies(databases, clock, events)

    payload = _make_payload(train_hook="hok_train_target", eval_hook="hok_eval_other")
    pol = await store.store("prj_1", payload, proposal_id="prop_1")

    # Suspend for hook
    await store.suspend_for_hook("prj_1", "hok_train_target", reason="Pinned file changed")

    suspended = await store.get("prj_1", pol.id)
    assert suspended is not None
    assert suspended.status == "suspended"
    assert suspended.suspended_reason == "Pinned file changed"

    # Verify event recorded
    history = await events.history("prj_1", types=["policy.suspended"])
    assert any(e.type == "policy.suspended" for e in history)

    # Activate for hook
    await store.activate_for_hook("prj_1", "hok_train_target")
    activated = await store.get("prj_1", pol.id)
    assert activated is not None
    assert activated.status == "active"
    assert activated.suspended_reason is None


@pytest.mark.asyncio
async def test_training_policy_kind_warnings(tmp_path: Path) -> None:
    databases = Databases(DataPaths(tmp_path / "data"))
    store = SqliteTrainingPolicies(databases, FakeClock(), SqliteEventLog(databases))

    train_hook = _make_hook("hok_train", deploys=True, hook_type="command")
    hooks = FakeHooks([train_hook])
    kind = TrainingPolicyKind(store, hooks=hooks)  # type: ignore[arg-type]
    kind.cache_hook(train_hook)

    payload = _make_payload(train_hook="hok_train", eval_hook=None)
    warns = kind.warnings(payload)

    # Deploys warning
    assert any("deploys" in w for w in warns)
    # Run-time limit warning
    assert any("run-time limit" in w for w in warns)
    # Command hook warning
    assert any("Command hook" in w and "trq" in w for w in warns)


@pytest.mark.asyncio
async def test_training_policy_kind_apply_refuses_deploying_hook_without_rollback_rule(
    tmp_path: Path,
) -> None:
    databases = Databases(DataPaths(tmp_path / "data"))
    clock = FakeClock()
    events = SqliteEventLog(databases)
    store = SqliteTrainingPolicies(databases, clock, events)

    train_hook = _make_hook("hok_train", deploys=True)
    hooks = FakeHooks([train_hook])
    rules = FakeRules([])  # No rollback rule

    kind = TrainingPolicyKind(store, hooks=hooks, rules=rules)  # type: ignore[arg-type]
    payload = _make_payload(train_hook="hok_train", eval_hook=None)

    result = await kind.apply("prj_1", "prop_deploy", payload)
    assert result.applied == []
    assert "policy" in result.failed
    assert "rollback rule" in result.failed["policy"]

    # Verify nothing was stored
    pols = await store.list("prj_1")
    assert len(pols) == 0


@pytest.mark.asyncio
async def test_training_policy_kind_apply_succeeds_with_rollback_rule(tmp_path: Path) -> None:
    databases = Databases(DataPaths(tmp_path / "data"))
    clock = FakeClock()
    events = SqliteEventLog(databases)
    store = SqliteTrainingPolicies(databases, clock, events)

    train_hook = _make_hook("hok_train", deploys=True)
    hooks = FakeHooks([train_hook])

    rollback_rule = RuleView(
        id="rul_1",
        spec=RuleSpec(
            name="rollback_rule",
            why="test",
            model="churn_risk",
            terms=[],
            hook_id="hok_rb",
            hook_kind="rollback",
            limits=RuleLimits(),
        ),
        enabled=True,
        proposal_id="prop_r1",
        approved=datetime.now(UTC),
    )
    rules = FakeRules([rollback_rule])

    kind = TrainingPolicyKind(store, hooks=hooks, rules=rules)  # type: ignore[arg-type]
    payload = _make_payload(train_hook="hok_train", eval_hook=None)

    result = await kind.apply("prj_1", "prop_deploy", payload)
    assert len(result.applied) == 1
    assert result.applied[0].startswith("policy:pol_")
    assert result.failed == {}

    pols = await store.list("prj_1")
    assert len(pols) == 1
    assert pols[0].status == "active"


@pytest.mark.asyncio
async def test_training_policy_kind_apply_idempotent_and_supersedes(tmp_path: Path) -> None:
    databases = Databases(DataPaths(tmp_path / "data"))
    clock = FakeClock()
    events = SqliteEventLog(databases)
    store = SqliteTrainingPolicies(databases, clock, events)

    train_hook = _make_hook("hok_train", deploys=False)
    hooks = FakeHooks([train_hook])
    kind = TrainingPolicyKind(store, hooks=hooks)  # type: ignore[arg-type]

    payload1 = _make_payload()
    result1 = await kind.apply("prj_1", "prop_1", payload1)
    pol1_id = result1.applied[0].replace("policy:", "")

    # Idempotent re-apply returns same policy
    result1_repeat = await kind.apply("prj_1", "prop_1", payload1)
    assert result1_repeat.applied == result1.applied

    # Superseding apply with new proposal
    payload2 = _make_payload(eval_hook=None)
    result2 = await kind.apply("prj_1", "prop_2", payload2)
    pol2_id = result2.applied[0].replace("policy:", "")
    assert pol2_id != pol1_id

    pol1 = await store.get("prj_1", pol1_id)
    assert pol1 is not None
    assert pol1.status == "disabled"

    pol2 = await store.get("prj_1", pol2_id)
    assert pol2 is not None
    assert pol2.status == "active"


@pytest.mark.asyncio
async def test_policies_query_provider(tmp_path: Path) -> None:
    databases = Databases(DataPaths(tmp_path / "data"))
    clock = FakeClock()
    events = SqliteEventLog(databases)
    training = SqliteTraining(databases, events, clock)
    provider = PoliciesQueryProvider(training)

    assert provider.what == "policies"

    # Store 2 policies
    p1 = _make_payload(model="churn_risk")
    p2 = _make_payload(model="fraud_model")
    await training.policies_store.store("prj_1", p1, proposal_id="pr1")
    await training.policies_store.store("prj_1", p2, proposal_id="pr2")

    page = await provider.query("prj_1", MonitoringQuery(what="policies"))
    assert page.total == 2
    assert len(page.rows) == 2

    filtered = await provider.query("prj_1", MonitoringQuery(what="policies", model="churn_risk"))
    assert filtered.total == 1
    assert filtered.rows[0].payload.model == "churn_risk"


@pytest.mark.asyncio
async def test_policies_routes(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    services = app.state.services

    # 1. List initially empty
    resp = await client.get(f"/api/projects/{project_id}/training/policies")
    assert resp.status_code == 200
    assert resp.json() == []

    # 2. Store a policy
    payload = _make_payload()
    pol = await services.training.policies_store.store(project_id, payload, proposal_id="prop_api")

    # 3. List contains policy
    resp = await client.get(f"/api/projects/{project_id}/training/policies")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == pol.id
    assert data[0]["status"] == "active"

    # Filter by model
    resp_filtered = await client.get(
        f"/api/projects/{project_id}/training/policies", params={"model": "churn_risk"}
    )
    assert resp_filtered.status_code == 200
    assert len(resp_filtered.json()) == 1

    resp_empty = await client.get(
        f"/api/projects/{project_id}/training/policies", params={"model": "nonexistent"}
    )
    assert resp_empty.status_code == 200
    assert len(resp_empty.json()) == 0

    # 4. PUT enabled to disable
    resp_put = await client.put(
        f"/api/projects/{project_id}/training/policies/{pol.id}/enabled",
        json={"enabled": False},
    )
    assert resp_put.status_code == 200
    assert resp_put.json()["status"] == "disabled"

    # PUT enabled to re-enable
    resp_put2 = await client.put(
        f"/api/projects/{project_id}/training/policies/{pol.id}/enabled",
        json={"enabled": True},
    )
    assert resp_put2.status_code == 200
    assert resp_put2.json()["status"] == "active"

    # 404 for unknown policy
    resp_404 = await client.put(
        f"/api/projects/{project_id}/training/policies/pol_unknown/enabled",
        json={"enabled": True},
    )
    assert resp_404.status_code == 404
