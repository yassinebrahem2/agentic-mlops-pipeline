"""Tests for ProposalShape tool (Plan #712, Issue #713)."""

from __future__ import annotations

import json
from typing import Any, cast, get_args

import pytest

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import FileAccess, RiskClass, ToolContext, ToolFailure
from mlops_agents.agents.tools.proposal_shape import (
    PROPOSAL_SHAPE_TOOL_NAME as PROPOSAL_SHAPE,
)
from mlops_agents.agents.tools.proposal_shape import (
    ProposalShape,
    ProposalShapeArgs,
    ProposalShapeTool,
)
from mlops_agents.agents.tools.propose import ProposeArgs, ProposeTool
from mlops_agents.proposals.contract import (
    CONNECTIONS_KIND,
    HANDOFF_KIND,
    ConnectionsPayload,
    HandoffPayload,
)
from mlops_agents.proposals.kinds import ProposalKinds
from mlops_agents.proposals.payloads import Placeholder
from tests.conftest import STAGE_2_TOOLS, STAGE_3_HOOKS
from tests.proposals.conftest import ScriptedTestKind

CTX = ToolContext(
    project_id="prj_test",
    agent="onboarding",
    session_id="ses_1",
    turn_id="trn_1",
    files=cast(FileAccess, None),
    task=None,
    tool_call_id="call_1",
)


@pytest.fixture
def kinds_registry() -> ProposalKinds:
    registry = ProposalKinds()
    registry.register(ScriptedTestKind(name=CONNECTIONS_KIND, payload_model=ConnectionsPayload))
    registry.register(ScriptedTestKind(name=HANDOFF_KIND, payload_model=HandoffPayload))
    return registry


@pytest.mark.asyncio
async def test_proposal_shape_returns_json_schema(kinds_registry: ProposalKinds) -> None:
    assert ProposalShape is ProposalShapeTool
    tool = ProposalShapeTool(kinds_registry)
    assert tool.risk == RiskClass.READ
    assert tool.name == PROPOSAL_SHAPE

    # "connections"
    conn_output = await tool.run(CTX, ProposalShapeArgs(kind=CONNECTIONS_KIND))
    assert conn_output.is_error is False
    conn_schema = json.loads(conn_output.content)
    assert conn_schema == ConnectionsPayload.model_json_schema()
    assert "connections" in conn_schema["properties"]

    # "handoff"
    handoff_output = await tool.run(CTX, ProposalShapeArgs(kind=HANDOFF_KIND))
    assert handoff_output.is_error is False
    handoff_schema = json.loads(handoff_output.content)
    assert handoff_schema == HandoffPayload.model_json_schema()
    assert "models" in handoff_schema["properties"]

    # Callable / helper interface
    assert tool(CONNECTIONS_KIND) == ConnectionsPayload.model_json_schema()
    assert tool(HANDOFF_KIND) == HandoffPayload.model_json_schema()


@pytest.mark.asyncio
async def test_unknown_kind_lists_available_kinds(kinds_registry: ProposalKinds) -> None:
    tool = ProposalShapeTool(kinds_registry)
    output = await tool.run(CTX, ProposalShapeArgs(kind="unknown_kind"))

    assert output.is_error is True
    assert "Unknown proposal kind" in output.content
    assert CONNECTIONS_KIND in output.content
    assert HANDOFF_KIND in output.content


def test_handoff_schema_carries_on_call_and_placeholder_rules() -> None:
    schema = HandoffPayload.model_json_schema()
    schema_text = json.dumps(schema)

    # 1. On-call rule: empty list [] when nobody is known
    assert "[]" in schema_text
    assert "nobody is known" in schema_text.lower() or "unknown" in schema_text.lower()

    # 2. Placeholders rule: drawn from Placeholder constant
    for placeholder in get_args(Placeholder):
        assert placeholder in schema_text


def test_propose_description_and_validation_error_pointer(kinds_registry: ProposalKinds) -> None:
    from unittest.mock import MagicMock

    from mlops_agents.proposals.service import ProposalsService

    proposals_svc = MagicMock(spec=ProposalsService)
    propose_tool = ProposeTool(proposals_svc, kinds_registry)

    # Description points to ProposalShape
    assert PROPOSAL_SHAPE in propose_tool.description

    # Validation failure points to ProposalShape(kind)
    bad_payload: dict[str, Any] = {"connections": "not a list"}
    args = ProposeArgs(kind=CONNECTIONS_KIND, payload=bad_payload)

    with pytest.raises(ToolFailure) as exc_info:
        import asyncio

        asyncio.run(propose_tool.preview(CTX, args))

    err_msg = str(exc_info.value)
    assert PROPOSAL_SHAPE in err_msg
    assert CONNECTIONS_KIND in err_msg


def test_definitions_with_propose_list_proposal_shape() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    expected_agents = {
        "onboarding",
        "data-quality",
        "deployment",
        "model-performance",
        "retraining",
    }
    for agent_name in expected_agents:
        defn = catalog.get(agent_name)
        assert PROPOSAL_SHAPE in defn.tools, f"{agent_name} missing {PROPOSAL_SHAPE} in tools"
        low = defn.prompt.lower()
        assert "proposalshape" in low, f"{agent_name} prompt does not mention ProposalShape"


def test_proposal_shape_registered_in_default_registry(kinds_registry: ProposalKinds) -> None:
    registry = default_registry(proposal_kinds=kinds_registry)
    assert PROPOSAL_SHAPE in registry.names()
    tool = registry.get(PROPOSAL_SHAPE)
    assert tool is not None
    assert isinstance(tool, ProposalShapeTool)
