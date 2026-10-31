"""Tests for RequestFix and RecordFixChoice tools (Task 1, #486)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mlops_agents.agents.policy.table import decide
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import RiskClass, ToolContext, ToolFailure
from mlops_agents.agents.tools.names import RECORD_FIX_CHOICE, REQUEST_FIX
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.storage.databases import Databases
from mlops_agents.training.constants import RETRAINING_AGENT
from mlops_agents.training.fixes import SqliteFixes
from mlops_agents.training.tools import (
    RecordFixChoice,
    RecordFixChoiceArgs,
    RequestFix,
    RequestFixArgs,
)
from tests.agents.gate_helpers import FakeFileAccess
from tests.providers.fake_clock import FakeClock
from tests.training.test_fixes_store import FakeAgentWaker, FakeIncidents, make_incident


def make_ctx(project_id: str = "prj_test", agent: str = "model-performance") -> ToolContext:
    return ToolContext(
        project_id=project_id,
        agent=agent,
        session_id="ses_1",
        turn_id="trn_1",
        files=FakeFileAccess(),
        task=None,
        tool_call_id="call_1",
        authority=None,
    )


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(start=datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def fake_waker() -> FakeAgentWaker:
    return FakeAgentWaker()


@pytest.fixture
def fake_incidents() -> FakeIncidents:
    inc = make_incident("inc_test", model="churn_predictor")
    return FakeIncidents({"inc_test": inc})


@pytest.fixture
def fixes_service(
    databases: Databases,
    fake_incidents: FakeIncidents,
    fake_waker: FakeAgentWaker,
    fake_clock: FakeClock,
) -> SqliteFixes:
    events = SqliteEventLog(databases)
    return SqliteFixes(
        databases=databases,
        incidents=fake_incidents,  # type: ignore[arg-type]
        waker=fake_waker,
        events=events,
        clock=fake_clock,
    )


async def test_request_fix_tool_properties_and_success(
    fixes_service: SqliteFixes,
) -> None:
    tool = RequestFix(fixes_service)

    assert tool.name == REQUEST_FIX
    assert tool.risk == RiskClass.READ

    ctx = make_ctx(agent="model-performance")
    args = RequestFixArgs(incident_id="inc_test", evidence="Drift score 0.42 exceeds threshold")

    action = await tool.action_call(args, ctx)
    assert action is None

    out = await tool.run(ctx, args)
    assert out.content.startswith("Fix fix_")
    assert "is requested." in out.content


async def test_request_fix_tool_domain_error_raises_tool_failure(
    fixes_service: SqliteFixes,
) -> None:
    tool = RequestFix(fixes_service)
    ctx = make_ctx()
    args = RequestFixArgs(incident_id="inc_nonexistent", evidence="some evidence")

    with pytest.raises(ToolFailure) as exc_info:
        await tool.run(ctx, args)
    assert "Incident 'inc_nonexistent' not found." in str(exc_info.value)


async def test_record_fix_choice_tool_properties_and_success(
    fixes_service: SqliteFixes,
) -> None:
    # First create a fix
    fix = await fixes_service.request("prj_test", "inc_test", "model-performance", "evidence")

    tool = RecordFixChoice(fixes_service)
    assert tool.name == RECORD_FIX_CHOICE
    assert tool.risk == RiskClass.READ

    ctx_retrain = make_ctx(agent=RETRAINING_AGENT)
    args = RecordFixChoiceArgs(
        fix_id=fix.id,
        choice="retrain",
        why="Model weights are outdated.",
        why_not={
            "re_threshold": "Distribution has changed completely.",
            "recalibrate": "Calibration won't fix poor ranking.",
        },
    )

    action = await tool.action_call(args, ctx_retrain)
    assert action is None

    out = await tool.run(ctx_retrain, args)
    assert out.content == f"Fix {fix.id} is chosen."


async def test_record_fix_choice_tool_domain_error_raises_tool_failure(
    fixes_service: SqliteFixes,
) -> None:
    tool = RecordFixChoice(fixes_service)
    ctx = make_ctx(agent="data-quality")  # unauthorized agent
    args = RecordFixChoiceArgs(
        fix_id="fix_fake",
        choice="retrain",
        why="why",
        why_not={"re_threshold": "no", "recalibrate": "no"},
    )

    with pytest.raises(ToolFailure) as exc_info:
        await tool.run(ctx, args)
    assert "Only agent 'retraining' can choose a fix" in str(exc_info.value)


def test_tools_registered_in_default_registry_and_allowed_at_all_levels(
    fixes_service: SqliteFixes,
) -> None:
    registry_without = default_registry()
    assert REQUEST_FIX not in registry_without.names()
    assert RECORD_FIX_CHOICE not in registry_without.names()

    registry_with = default_registry(fixes=fixes_service)
    assert REQUEST_FIX in registry_with.names()
    assert RECORD_FIX_CHOICE in registry_with.names()

    req_tool = registry_with.get(REQUEST_FIX)
    rec_tool = registry_with.get(RECORD_FIX_CHOICE)
    assert req_tool is not None
    assert rec_tool is not None

    assert req_tool.risk == RiskClass.READ
    assert rec_tool.risk == RiskClass.READ

    # Verify allowed at every permission level
    for level in ("suggest", "ask", "autonomous", "bypass"):
        assert decide(req_tool.risk, level) == "allow"  # type: ignore[arg-type]
        assert decide(rec_tool.risk, level) == "allow"  # type: ignore[arg-type]
