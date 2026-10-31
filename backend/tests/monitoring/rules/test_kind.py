"""Unit tests for RuleKind proposal kind."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mlops_agents.monitoring.expr import Compare
from mlops_agents.monitoring.hooks.contract import HookView
from mlops_agents.monitoring.rules.contract import (
    RuleLimits,
    RulePayload,
    RuleSpec,
    RuleTerm,
    RuleView,
)
from mlops_agents.monitoring.rules.kind import RuleKind
from mlops_agents.proposals.contract import RULE_KIND, ProposalKind


def _sample_spec(
    name: str = "auto-rollback",
    why: str = "Rollback when drift is high",
    model: str | None = "churn",
    hook_id: str = "hok_test1",
    hook_kind: str = "rollback",
    per_incident: int = 1,
    per_day: int = 2,
) -> RuleSpec:
    return RuleSpec(
        name=name,
        why=why,
        model=model,
        terms=[
            RuleTerm(
                check_id="drift_check",
                model=model,
                when=Compare(op="gt", field="value", value=0.5),
                window_minutes=60,
                quantifier="latest",
                min_records=1,
            )
        ],
        hook_id=hook_id,
        hook_kind=hook_kind,  # type: ignore[arg-type]
        limits=RuleLimits(per_incident=per_incident, per_day=per_day),
    )


def _sample_hook_view(
    hook_id: str = "hok_test1",
    name: str = "rollback-hook",
    model: str | None = "churn",
    kind: str = "rollback",
    deploys: bool = False,
) -> HookView:
    return HookView.model_validate(
        {
            "id": hook_id,
            "name": name,
            "model": model,
            "spec": {
                "kind": kind,
                "type": "command",
                "template": "python rollback.py",
                "workflow": None,
                "inputs": {},
                "placeholders": [],
                "deploys": deploys,
                "entry_files": [],
            },
            "status": "active",
            "suspended_reason": None,
            "credential_set": True,
            "pins": [],
            "approved_commit": "abc",
            "deploys_verified": None,
            "source": "proposal",
            "created": "2026-10-31T00:00:00Z",
        }
    )


def test_rule_kind_conforms_to_proposal_kind() -> None:
    rules = MagicMock()
    hooks = MagicMock()
    kind: ProposalKind = RuleKind(rules=rules, hooks=hooks)
    assert kind.name == RULE_KIND
    assert kind.name == "rule"
    assert kind.payload_model is RulePayload
    for method in ("key", "preview", "warnings", "credentials_needed", "apply"):
        assert callable(getattr(kind, method))


def test_key() -> None:
    rules = MagicMock()
    hooks = MagicMock()
    kind = RuleKind(rules=rules, hooks=hooks)

    payload_with_model = RulePayload(spec=_sample_spec(name="rb", model="churn"))
    assert kind.key(payload_with_model) == "churn:rb"

    payload_no_model = RulePayload(spec=_sample_spec(name="rb", model=None))
    assert kind.key(payload_no_model) == "rb"


def test_preview() -> None:
    rules = MagicMock()
    hooks = MagicMock()
    kind = RuleKind(rules=rules, hooks=hooks)

    payload = RulePayload(spec=_sample_spec())
    prev = kind.preview(payload)
    assert prev.kind == "text"
    assert "auto-rollback" in prev.title or "auto-rollback" in prev.body
    assert "churn" in prev.body
    assert "Rollback when drift is high" in prev.body
    assert "hok_test1" in prev.body
    assert "rollback" in prev.body
    assert "drift_check" in prev.body
    assert "per incident" in prev.body


def test_warnings() -> None:
    rules = MagicMock()
    hooks = MagicMock()
    # Hook does not deploy
    hooks.hook_deploys = MagicMock(return_value=False)
    kind = RuleKind(rules=rules, hooks=hooks)
    payload = RulePayload(spec=_sample_spec(hook_id="hok_safe"))
    assert kind.warnings(payload) == []

    # Hook deploys
    hooks.hook_deploys = MagicMock(return_value=True)
    payload_deploy = RulePayload(spec=_sample_spec(hook_id="hok_deployer"))
    assert kind.warnings(payload_deploy) == ["Rule hook 'hok_deployer' deploys."]


def test_credentials_needed() -> None:
    rules = MagicMock()
    hooks = MagicMock()
    kind = RuleKind(rules=rules, hooks=hooks)
    payload = RulePayload(spec=_sample_spec())
    assert kind.credentials_needed(payload) == []


@pytest.mark.asyncio
async def test_apply_success() -> None:
    rules = AsyncMock()
    hooks = AsyncMock()
    spec = _sample_spec(name="rb", model="churn", hook_id="hok_1", hook_kind="rollback")
    payload = RulePayload(spec=spec)

    hook_view = _sample_hook_view(hook_id="hok_1", model="churn", kind="rollback")
    hooks.get.return_value = hook_view

    rule_view = RuleView.model_validate(
        {
            "id": "rul_123",
            "spec": spec.model_dump(),
            "enabled": True,
            "proposal_id": "prp_1",
            "approved": "2026-10-31T01:00:00Z",
        }
    )
    rules.store.return_value = rule_view

    kind = RuleKind(rules=rules, hooks=hooks)
    result = await kind.apply("prj_1", "prp_1", payload)

    assert result.applied == ["rule:churn:rb"]
    assert result.failed == {}
    rules.store.assert_awaited_once_with("prj_1", spec=spec, proposal_id="prp_1")

    # Second apply (idempotent)
    result2 = await kind.apply("prj_1", "prp_1", payload)
    assert result2.applied == ["rule:churn:rb"]
    assert result2.failed == {}
    assert rules.store.await_count == 2


@pytest.mark.asyncio
async def test_apply_fails_missing_hook() -> None:
    rules = AsyncMock()
    hooks = AsyncMock()
    spec = _sample_spec(name="rb", model="churn", hook_id="hok_missing", hook_kind="rollback")
    payload = RulePayload(spec=spec)

    hooks.get.return_value = None

    kind = RuleKind(rules=rules, hooks=hooks)
    result = await kind.apply("prj_1", "prp_1", payload)

    assert result.applied == []
    assert "rule:churn:rb" in result.failed
    assert "not found" in result.failed["rule:churn:rb"]
    rules.store.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_fails_wrong_hook_kind() -> None:
    rules = AsyncMock()
    hooks = AsyncMock()
    spec = _sample_spec(name="rb", model="churn", hook_id="hok_1", hook_kind="rollback")
    payload = RulePayload(spec=spec)

    # Hook has kind "rerun", but rule requires "rollback"
    hook_view = _sample_hook_view(hook_id="hok_1", model="churn", kind="rerun")
    hooks.get.return_value = hook_view

    kind = RuleKind(rules=rules, hooks=hooks)
    result = await kind.apply("prj_1", "prp_1", payload)

    assert result.applied == []
    assert "rule:churn:rb" in result.failed
    assert "kind" in result.failed["rule:churn:rb"]
    rules.store.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_fails_wrong_model() -> None:
    rules = AsyncMock()
    hooks = AsyncMock()
    spec = _sample_spec(name="rb", model="churn", hook_id="hok_1", hook_kind="rollback")
    payload = RulePayload(spec=spec)

    # Hook has model "fraud", but rule is for "churn"
    hook_view = _sample_hook_view(hook_id="hok_1", model="fraud", kind="rollback")
    hooks.get.return_value = hook_view

    kind = RuleKind(rules=rules, hooks=hooks)
    result = await kind.apply("prj_1", "prp_1", payload)

    assert result.applied == []
    assert "rule:churn:rb" in result.failed
    assert "model" in result.failed["rule:churn:rb"]
    rules.store.assert_not_awaited()


@pytest.mark.parametrize(
    "invalid_kwargs,expected_substr",
    [
        ({"terms": []}, "terms"),
        (
            {
                "terms": [
                    RuleTerm(
                        check_id="chk",
                        when=Compare(op="gt", field="value", value=0.5),
                        window_minutes=60,
                        quantifier="latest",
                        min_records=0,
                    )
                ]
            },
            "min_records",
        ),
        (
            {
                "terms": [
                    RuleTerm(
                        check_id="chk",
                        when=Compare(op="gt", field="value", value=0.5),
                        window_minutes=0,
                        quantifier="latest",
                        min_records=1,
                    )
                ]
            },
            "window_minutes",
        ),
        ({"limits": RuleLimits(per_incident=0, per_day=1)}, "per_incident"),
        ({"limits": RuleLimits(per_incident=1, per_day=0)}, "per_day"),
    ],
)
@pytest.mark.asyncio
async def test_apply_rejects_invalid_specs(
    invalid_kwargs: dict[str, Any],
    expected_substr: str,
) -> None:
    rules = AsyncMock()
    hooks = AsyncMock()
    hook_view = _sample_hook_view(hook_id="hok_1", model="churn", kind="rollback")
    hooks.get.return_value = hook_view

    spec_dict: dict[str, Any] = {
        "name": "rb",
        "why": "test",
        "model": "churn",
        "terms": [
            RuleTerm(
                check_id="drift_check",
                model="churn",
                when=Compare(op="gt", field="value", value=0.5),
                window_minutes=60,
                quantifier="latest",
                min_records=1,
            )
        ],
        "hook_id": "hok_1",
        "hook_kind": "rollback",
        "limits": RuleLimits(per_incident=1, per_day=1),
    }
    spec_dict.update(invalid_kwargs)
    spec = RuleSpec(**spec_dict)
    payload = RulePayload(spec=spec)

    kind = RuleKind(rules=rules, hooks=hooks)
    result = await kind.apply("prj_1", "prp_1", payload)

    assert result.applied == []
    assert "rule:churn:rb" in result.failed
    assert expected_substr.lower() in result.failed["rule:churn:rb"].lower()
    rules.store.assert_not_awaited()
