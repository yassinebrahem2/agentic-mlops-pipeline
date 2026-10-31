"""Tests for RuleSeverity (Task 4 of Plan #464)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from mlops_agents.monitoring.expectations.contract import SECOND_DEADLINE_KEY
from mlops_agents.monitoring.incidents.contract import (
    IncidentView,
    SignalView,
    cause_key,
)
from mlops_agents.monitoring.incidents.severity import (
    RuleSeverity,
    has_path_to_model_stage,
    parse_cause_key,
)
from mlops_agents.proposals.payloads import (
    HandoffPayload,
    PipelineEdge,
)
from tests.proposals.samples import handoff as sample_handoff_dict
from tests.proposals.samples import model as sample_model_dict


def test_parse_cause_key() -> None:
    assert parse_cause_key("9:data_feed:m:churn") == ("data_feed", "churn")
    assert parse_cause_key("9:data_feed:none") == ("data_feed", None)
    assert parse_cause_key("14:nested:subject:none") == ("nested:subject", None)
    assert parse_cause_key("0::none") == ("", None)
    assert parse_cause_key("invalid") == ("", None)


def _make_incident(
    id: str = "inc_1",
    category: str = "data_quality",
    subtype: str | None = None,
    subject: str = "test_table",
    model: str | None = None,
    severity: str = "medium",
    status: str = "open",
    cause: str | None = None,
    issues: list[int] | None = None,
    prs: list[int] | None = None,
) -> IncidentView:
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    c_key = cause_key(subject, model)
    return IncidentView(
        id=id,
        category=category,  # type: ignore[arg-type]
        subtype=subtype,
        severity=severity,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        cause=cause,
        cause_key=c_key,
        model=model,
        started_at=now,
        detected_at=now,
        issues=issues or [],
        prs=prs or [],
    )


@pytest.mark.anyio
async def test_base_severity() -> None:
    fake_handoffs = AsyncMock()
    fake_handoffs.current.return_value = None
    policy = RuleSeverity(handoffs=fake_handoffs)

    inc = _make_incident(category="data_quality", subtype=None)
    decision = await policy.decide("proj_1", inc)

    assert decision.severity == "medium"
    assert decision.reasons == ["Base severity"]


@pytest.mark.anyio
async def test_rule_1_subtype_failed_scoring() -> None:
    fake_handoffs = AsyncMock()
    fake_handoffs.current.return_value = None
    policy = RuleSeverity(handoffs=fake_handoffs)

    inc = _make_incident(category="operational", subtype="failed_scoring")
    decision = await policy.decide("proj_1", inc)

    assert decision.severity == "critical"
    assert any("failed_scoring" in r for r in decision.reasons)


@pytest.mark.anyio
async def test_rule_1_subtype_bad_deploy() -> None:
    fake_handoffs = AsyncMock()
    fake_handoffs.current.return_value = None
    policy = RuleSeverity(handoffs=fake_handoffs)

    inc = _make_incident(category="operational", subtype="bad_deploy")
    decision = await policy.decide("proj_1", inc)

    assert decision.severity == "critical"
    assert any("bad_deploy" in r for r in decision.reasons)


@pytest.mark.anyio
async def test_rule_2_silence_feed_production_depends_on() -> None:
    # Pipeline: feed_stage -> transform_stage -> score_stage (lists model)
    pipeline = [
        {
            "name": "feed_stage",
            "entry": "feed.py",
            "inputs": ["raw_feed"],
            "outputs": ["clean_data"],
        },
        {
            "name": "transform_stage",
            "entry": "trans.py",
            "inputs": ["clean_data"],
            "outputs": ["features"],
        },
        {
            "name": "score_stage",
            "entry": "score.py",
            "inputs": ["features", "churn_model"],
            "outputs": ["preds"],
        },
    ]
    edges = [
        {"from_stage": "feed_stage", "to_stage": "transform_stage"},
        {"from_stage": "transform_stage", "to_stage": "score_stage"},
    ]
    m_dict = sample_model_dict(name="churn_model")
    handoff_dict = sample_handoff_dict(pipeline=pipeline, edges=edges, models=[m_dict])
    payload = HandoffPayload.model_validate(handoff_dict)

    fake_handoff_view = AsyncMock()
    fake_handoff_view.payload = payload
    fake_handoffs = AsyncMock()
    fake_handoffs.current.return_value = fake_handoff_view

    policy = RuleSeverity(handoffs=fake_handoffs)

    # Silence incident for raw_feed
    inc = _make_incident(
        category="data_quality",
        subtype="silence",
        subject="raw_feed",
        model=None,
    )
    decision = await policy.decide("proj_1", inc)

    assert decision.severity == "high"
    assert "Silence of feed or job that production depends on" in decision.reasons


@pytest.mark.anyio
async def test_rule_2_silence_second_deadline_critical() -> None:
    pipeline = [
        {
            "name": "feed_stage",
            "entry": "feed.py",
            "inputs": ["raw_feed"],
            "outputs": ["clean_data"],
        },
        {
            "name": "score_stage",
            "entry": "score.py",
            "inputs": ["clean_data", "churn_model"],
            "outputs": ["preds"],
        },
    ]
    edges = [
        {"from_stage": "feed_stage", "to_stage": "score_stage"},
    ]
    m_dict = sample_model_dict(name="churn_model")
    handoff_dict = sample_handoff_dict(pipeline=pipeline, edges=edges, models=[m_dict])
    payload = HandoffPayload.model_validate(handoff_dict)

    fake_handoff_view = AsyncMock()
    fake_handoff_view.payload = payload
    fake_handoffs = AsyncMock()
    fake_handoffs.current.return_value = fake_handoff_view

    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    fake_incidents = AsyncMock()
    fake_signal = SignalView(
        id="sig_1",
        incident_id="inc_1",
        kind="silence",
        category="data_quality",
        subject="raw_feed",
        ran_at=now,
        seen_at=now,
        cause_key=cause_key("raw_feed", None),
        detail={SECOND_DEADLINE_KEY: True},
    )
    fake_incidents.signals.return_value = [fake_signal]

    policy = RuleSeverity(handoffs=fake_handoffs, incidents=fake_incidents)

    inc = _make_incident(
        id="inc_1",
        category="data_quality",
        subtype="silence",
        subject="raw_feed",
        model=None,
    )
    decision = await policy.decide("proj_1", inc)

    assert decision.severity == "critical"
    assert "Second deadline missed for feed or job that production depends on" in decision.reasons


@pytest.mark.anyio
async def test_rule_2_feed_not_connected_to_model_stays_medium() -> None:
    pipeline = [
        {
            "name": "feed_stage",
            "entry": "feed.py",
            "inputs": ["raw_feed"],
            "outputs": ["clean_data"],
        },
        {
            "name": "unrelated_stage",
            "entry": "unrelated.py",
            "inputs": ["unrelated_feed"],
            "outputs": ["unrelated_out"],
        },
        {
            "name": "score_stage",
            "entry": "score.py",
            "inputs": ["clean_data", "churn_model"],
            "outputs": ["preds"],
        },
    ]
    edges = [
        {"from_stage": "feed_stage", "to_stage": "score_stage"},
    ]
    m_dict = sample_model_dict(name="churn_model")
    handoff_dict = sample_handoff_dict(pipeline=pipeline, edges=edges, models=[m_dict])
    payload = HandoffPayload.model_validate(handoff_dict)

    fake_handoff_view = AsyncMock()
    fake_handoff_view.payload = payload
    fake_handoffs = AsyncMock()
    fake_handoffs.current.return_value = fake_handoff_view

    policy = RuleSeverity(handoffs=fake_handoffs)

    inc = _make_incident(
        category="data_quality",
        subtype="silence",
        subject="unrelated_feed",
        model=None,
    )
    decision = await policy.decide("proj_1", inc)

    assert decision.severity == "medium"
    assert decision.reasons == ["Base severity"]


def test_has_path_to_model_stage_with_cycle() -> None:
    # Cycle between s1 and s2, with s2 leading to s3
    edges = [
        PipelineEdge(from_stage="s1", to_stage="s2"),
        PipelineEdge(from_stage="s2", to_stage="s1"),
        PipelineEdge(from_stage="s2", to_stage="s3"),
    ]
    assert has_path_to_model_stage(["s1"], {"s3"}, edges) is True
    assert has_path_to_model_stage(["s1"], {"unknown"}, edges) is False


@pytest.mark.anyio
async def test_rule_3_model_performance_cost_ratio_raises() -> None:
    m_dict = sample_model_dict(name="churn_model")
    m_dict["profile"]["cost_ratio"] = 3.5
    handoff_dict = sample_handoff_dict(models=[m_dict])
    payload = HandoffPayload.model_validate(handoff_dict)

    fake_handoff_view = AsyncMock()
    fake_handoff_view.payload = payload
    fake_handoffs = AsyncMock()
    fake_handoffs.current.return_value = fake_handoff_view

    policy = RuleSeverity(handoffs=fake_handoffs)

    # Base severity medium -> raised one level to high
    inc = _make_incident(
        category="model_performance",
        subject="predictions",
        model="churn_model",
    )
    decision = await policy.decide("proj_1", inc)

    assert decision.severity == "high"
    assert "Model performance error cost ratio (3.5) raises severity" in decision.reasons


@pytest.mark.anyio
async def test_rule_3_model_performance_cost_ratio_low_no_raise() -> None:
    m_dict = sample_model_dict(name="churn_model")
    m_dict["profile"]["cost_ratio"] = 1.0  # not > 1.0
    handoff_dict = sample_handoff_dict(models=[m_dict])
    payload = HandoffPayload.model_validate(handoff_dict)

    fake_handoff_view = AsyncMock()
    fake_handoff_view.payload = payload
    fake_handoffs = AsyncMock()
    fake_handoffs.current.return_value = fake_handoff_view

    policy = RuleSeverity(handoffs=fake_handoffs)

    inc = _make_incident(
        category="model_performance",
        subject="predictions",
        model="churn_model",
    )
    decision = await policy.decide("proj_1", inc)

    assert decision.severity == "medium"
    assert decision.reasons == ["Base severity"]
