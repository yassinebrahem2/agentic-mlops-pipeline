"""Tests for stage 4-5 proposal payloads, key functions, kind names, and ProposalView."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from mlops_agents.monitoring.expr import Compare
from mlops_agents.monitoring.hooks.contract import CommandSpec, HookProposal, HooksPayload
from mlops_agents.monitoring.incidents.contract import MaintenancePayload, WindowSpec
from mlops_agents.monitoring.results.contract import ParserPayload, ParserSpec, ResultRecord
from mlops_agents.monitoring.rules.contract import RuleLimits, RulePayload, RuleSpec, RuleTerm
from mlops_agents.promotion.contract import PromotionPayload
from mlops_agents.proposals.contract import (
    CONNECTIONS_KIND,
    HANDOFF_KIND,
    HOOKS_KIND,
    MAINTENANCE_KIND,
    PARSER_KIND,
    PROMOTION_KIND,
    RULE_KIND,
    TRAINING_POLICY_KIND,
    ConnectionsProposalView,
    HandoffProposalView,
    HooksProposalView,
    MaintenanceProposalView,
    ParserProposalView,
    PromotionProposalView,
    ProposalView,
    RuleProposalView,
    TrainingPolicyProposalView,
    hooks_key,
    maintenance_key,
    parser_key,
    promotion_key,
    rule_key,
    training_policy_key,
)
from mlops_agents.training.contract import TrainingPolicyPayload, WindowRule
from tests.proposals.samples import command, connections, handoff


def _now() -> datetime:
    return datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def _sample_parser_payload() -> ParserPayload:
    from mlops_agents.monitoring.results.contract import ParserCode

    spec = ParserSpec(source_id="file:conn:logs/*.json", code=ParserCode(script="pass"))
    rec = ResultRecord(
        check_id="chk_1",
        origin="agent",
        ran_at=_now(),
        fired=False,
        category="data_quality",
        subject="sub",
        detail={},
        source_ref="ref",
    )
    return ParserPayload(spec=spec, sample_input="{}", sample_records=[rec])


def _sample_rule_payload(model: str | None = "model_a") -> RulePayload:
    term = RuleTerm(
        check_id="chk_1",
        model=model,
        when=Compare(op="gt", field="loss", value=0.5),
        window_minutes=60,
        quantifier="latest",
    )
    spec = RuleSpec(
        name="high_loss",
        why="Loss exceeded threshold",
        model=model,
        terms=[term],
        hook_id="hok_1",
        hook_kind="rollback",
        limits=RuleLimits(per_incident=1, per_day=2),
    )
    return RulePayload(spec=spec)


def _sample_hooks_payload() -> HooksPayload:
    cmd = CommandSpec.model_validate(command(kind="rollback"))
    hp = HookProposal(name="rollback-1", model="model_a", spec=cmd, why="Rollback hook")
    return HooksPayload(hooks=[hp])


def _sample_maintenance_payload() -> MaintenancePayload:
    window = WindowSpec(
        start=_now(),
        end=_now(),
        scope=["feed_1", "job_2"],
        reason="Scheduled upgrade",
    )
    return MaintenancePayload(window=window)


def _sample_training_policy_payload() -> TrainingPolicyPayload:
    return TrainingPolicyPayload(
        model="model_a",
        train_hook_id="hok_train",
        evaluate_hook_id="hok_eval",
        window=WindowRule(kind="trailing_days", days=7, start=None, end=None),
        min_interval_hours=24,
        max_runs_per_week=3,
        run_time_limit_minutes=60,
    )


def _sample_promotion_payload() -> PromotionPayload:
    return PromotionPayload(
        model="model_a",
        candidate_version="v2",
        champion_version="v1",
        report_id="rep_1",
        deploy_hook_id="hok_deploy",
    )


def test_payloads_forbid_extra_keys() -> None:
    p_parser = _sample_parser_payload()
    with pytest.raises(ValidationError):
        ParserPayload.model_validate({**p_parser.model_dump(), "extra_key": "bad"})

    p_rule = _sample_rule_payload()
    with pytest.raises(ValidationError):
        RulePayload.model_validate({**p_rule.model_dump(), "extra_key": "bad"})

    cmd = CommandSpec.model_validate(command(kind="rollback"))
    with pytest.raises(ValidationError):
        HookProposal.model_validate(
            {"name": "h", "model": None, "spec": cmd, "why": "w", "extra": 1}
        )

    p_hooks = _sample_hooks_payload()
    with pytest.raises(ValidationError):
        HooksPayload.model_validate({**p_hooks.model_dump(), "extra_key": "bad"})

    with pytest.raises(ValidationError):
        WindowSpec.model_validate(
            {"start": _now(), "end": _now(), "scope": [], "reason": "r", "extra": 1}
        )

    p_maint = _sample_maintenance_payload()
    with pytest.raises(ValidationError):
        MaintenancePayload.model_validate({**p_maint.model_dump(), "extra_key": "bad"})


def test_key_functions() -> None:
    assert parser_key(_sample_parser_payload()) == "file:conn:logs/*.json"
    assert rule_key(_sample_rule_payload(model="model_a")) == "model_a:high_loss"
    assert rule_key(_sample_rule_payload(model=None)) == "high_loss"
    assert hooks_key(_sample_hooks_payload()) == "project"

    maint = _sample_maintenance_payload()
    expected_maint_key = f"{maint.window.reason}:{maint.window.start.isoformat()}"
    assert maintenance_key(maint) == expected_maint_key

    assert training_policy_key(_sample_training_policy_payload()) == "model_a"
    assert promotion_key(_sample_promotion_payload()) == "model_a"


def test_proposal_view_validates_each_kind_and_refuses_payload_mismatch() -> None:
    now = _now()
    base_args: dict[str, Any] = {
        "id": "prp_1",
        "key": "k",
        "warnings": [],
        "status": "pending",
        "agent": "diagnoser",
        "approval_id": None,
        "turn_id": "trn_1",
        "tool_call_id": "call_1",
        "result": None,
        "slots": [],
        "created": now,
    }

    def make_view(kind: str, payload: Any) -> ProposalView:
        return ProposalView.model_validate({**base_args, "kind": kind, "payload": payload})

    # 1. connections
    pv_conn = make_view(CONNECTIONS_KIND, connections())
    assert pv_conn.kind == CONNECTIONS_KIND
    assert isinstance(pv_conn.root, ConnectionsProposalView)
    assert pv_conn.id == "prp_1"
    assert pv_conn.created == now

    # 2. handoff
    pv_handoff = make_view(HANDOFF_KIND, handoff())
    assert pv_handoff.kind == HANDOFF_KIND
    assert isinstance(pv_handoff.root, HandoffProposalView)

    # 3. parser
    pv_parser = make_view(PARSER_KIND, _sample_parser_payload())
    assert pv_parser.kind == PARSER_KIND
    assert isinstance(pv_parser.root, ParserProposalView)
    assert pv_parser.payload.spec.source_id == "file:conn:logs/*.json"

    # 4. rule
    pv_rule = make_view(RULE_KIND, _sample_rule_payload())
    assert pv_rule.kind == RULE_KIND
    assert isinstance(pv_rule.root, RuleProposalView)

    # 5. hooks
    pv_hooks = make_view(HOOKS_KIND, _sample_hooks_payload())
    assert pv_hooks.kind == HOOKS_KIND
    assert isinstance(pv_hooks.root, HooksProposalView)

    # 6. maintenance
    pv_maint = make_view(MAINTENANCE_KIND, _sample_maintenance_payload())
    assert pv_maint.kind == MAINTENANCE_KIND
    assert isinstance(pv_maint.root, MaintenanceProposalView)

    # 7. training_policy
    pv_train = make_view(TRAINING_POLICY_KIND, _sample_training_policy_payload())
    assert pv_train.kind == TRAINING_POLICY_KIND
    assert isinstance(pv_train.root, TrainingPolicyProposalView)

    # 8. promotion
    pv_promo = make_view(PROMOTION_KIND, _sample_promotion_payload())
    assert pv_promo.kind == PROMOTION_KIND
    assert isinstance(pv_promo.root, PromotionProposalView)

    # Mismatch: kind parser with rule payload
    with pytest.raises(ValidationError):
        make_view(PARSER_KIND, _sample_rule_payload())

    # Unknown kind
    with pytest.raises(ValidationError):
        make_view("unknown", {})


def test_exported_proposal_view_schema_keeps_kind_discriminator() -> None:
    schema = ProposalView.model_json_schema()
    discriminator = schema.get("discriminator")
    assert discriminator is not None
    assert discriminator.get("propertyName") == "kind"
    assert set(discriminator.get("mapping", {})) == {
        CONNECTIONS_KIND,
        HANDOFF_KIND,
        PARSER_KIND,
        RULE_KIND,
        HOOKS_KIND,
        MAINTENANCE_KIND,
        TRAINING_POLICY_KIND,
        PROMOTION_KIND,
    }

