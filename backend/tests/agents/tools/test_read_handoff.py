"""Tests for ReadHandoffTool."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast, get_args

import pytest

from mlops_agents.agents.tools.base import FileAccess, RiskClass, ToolContext
from mlops_agents.agents.tools.read_handoff import (
    READ_HANDOFF_SECTION_CAP,
    ReadHandoffInput,
    ReadHandoffTool,
    SectionName,
)
from mlops_agents.proposals.contract import HandoffPayload, HandoffView
from tests.proposals.samples import handoff, model, profile


class FakeHandoffs:
    def __init__(self, current_view: HandoffView | None = None) -> None:
        self._view = current_view
        self.current_calls: list[str] = []

    async def current(self, project_id: str) -> HandoffView | None:
        self.current_calls.append(project_id)
        return self._view

    async def store(self, project_id: str, proposal_id: str, payload: HandoffPayload) -> int:
        raise NotImplementedError("ReadHandoff must not call store")


def _make_view(payload_dict: dict[str, Any] | None = None) -> HandoffView:
    p = HandoffPayload.model_validate(payload_dict or handoff())
    return HandoffView(
        proposal_id="prop_test_123",
        version=1,
        approved=datetime.now(UTC),
        payload=p,
    )


CTX = ToolContext(
    project_id="prj_test",
    agent="orchestrator",
    session_id="ses_test",
    turn_id="turn_test",
    files=cast(FileAccess, None),
    task=None,
    tool_call_id="call_test",
)


@pytest.mark.asyncio
async def test_read_handoff_no_handoff() -> None:
    fake = FakeHandoffs(current_view=None)
    tool = ReadHandoffTool(fake)
    output = await tool.run(CTX, ReadHandoffInput(section=None))
    assert output.is_error is False
    assert "Onboarding has not handed off" in output.content


@pytest.mark.asyncio
async def test_read_handoff_with_full_handoff_no_section_lists_every_section() -> None:
    view = _make_view()
    fake = FakeHandoffs(current_view=view)
    tool = ReadHandoffTool(fake)
    output = await tool.run(CTX, ReadHandoffInput(section=None))
    assert output.is_error is False
    for field in HandoffPayload.model_fields:
        assert field in output.content
    assert "models: 2" in output.content


@pytest.mark.asyncio
async def test_read_handoff_section_models() -> None:
    view = _make_view()
    fake = FakeHandoffs(current_view=view)
    tool = ReadHandoffTool(fake)
    output = await tool.run(CTX, ReadHandoffInput(section="models"))
    assert output.is_error is False
    assert "cancel" in output.content
    assert "noshow" in output.content
    assert "team_gate" in output.content


@pytest.mark.asyncio
async def test_read_handoff_section_operations() -> None:
    view = _make_view()
    fake = FakeHandoffs(current_view=view)
    tool = ReadHandoffTool(fake)
    output = await tool.run(CTX, ReadHandoffInput(section="operations"))
    assert output.is_error is False
    assert "scheduler" in output.content
    assert "feeds" in output.content
    assert "on_call" in output.content
    assert "checks" in output.content
    assert "results_location" in output.content


@pytest.mark.asyncio
async def test_read_handoff_section_over_cap_drops_trailing_items() -> None:
    models = [model(f"m_{i}") for i in range(50)]
    view = _make_view(handoff(models=models))
    fake = FakeHandoffs(current_view=view)
    tool = ReadHandoffTool(fake)
    output = await tool.run(CTX, ReadHandoffInput(section="models"))
    assert output.is_error is False
    assert "more items omitted" in output.content
    assert len(output.content) <= READ_HANDOFF_SECTION_CAP + 200


@pytest.mark.asyncio
async def test_read_handoff_single_item_over_cap_cut_at_cap() -> None:
    huge_objective = "X" * (READ_HANDOFF_SECTION_CAP + 1000)
    huge_model = model("huge", profile={**profile(), "objective": huge_objective})
    view = _make_view(handoff(models=[huge_model]))
    fake = FakeHandoffs(current_view=view)
    tool = ReadHandoffTool(fake)
    output = await tool.run(CTX, ReadHandoffInput(section="models"))
    assert output.is_error is False
    assert "cut at the cap" in output.content
    assert len(output.content) > 0


def test_section_literal_equals_handoff_payload_model_fields() -> None:
    section_values = set(get_args(SectionName))
    assert section_values == set(HandoffPayload.model_fields.keys())


def test_read_handoff_risk_and_readonly() -> None:
    tool = ReadHandoffTool()
    assert tool.risk == RiskClass.READ
    assert tool.name == "ReadHandoff"
