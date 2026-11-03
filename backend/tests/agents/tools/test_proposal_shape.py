"""Tests for ProposalShape tool (Plan #712, Issue #713)."""

from __future__ import annotations

import json
from typing import Any, cast, get_args

import jsonschema
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
from tests.fakes.onboarding_script import connections_payload, handoff_payload
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

    # "connections" - returned as compact JSON
    conn_output = await tool.run(CTX, ProposalShapeArgs(kind=CONNECTIONS_KIND))
    assert conn_output.is_error is False
    assert "\n" not in conn_output.content
    conn_schema = json.loads(conn_output.content)
    assert conn_schema == ConnectionsPayload.model_json_schema()
    assert "connections" in conn_schema["properties"]
    assert conn_output.content == json.dumps(conn_schema, separators=(",", ":"))

    # "handoff" - returned as compact JSON
    handoff_output = await tool.run(CTX, ProposalShapeArgs(kind=HANDOFF_KIND))
    assert handoff_output.is_error is False
    assert "\n" not in handoff_output.content
    handoff_schema = json.loads(handoff_output.content)
    assert handoff_schema == HandoffPayload.model_json_schema()
    assert "models" in handoff_schema["properties"]
    assert handoff_output.content == json.dumps(handoff_schema, separators=(",", ":"))


@pytest.mark.asyncio
async def test_payload_valid_against_model_validates_against_returned_schema(
    kinds_registry: ProposalKinds,
) -> None:
    tool = ProposalShapeTool(kinds_registry)

    # 1. Valid connections payload validates against returned schema
    conn_output = await tool.run(CTX, ProposalShapeArgs(kind=CONNECTIONS_KIND))
    assert conn_output.is_error is False
    conn_schema = json.loads(conn_output.content)
    valid_conn = connections_payload("/data/path")
    assert ConnectionsPayload.model_validate(valid_conn)
    jsonschema.validate(instance=valid_conn, schema=conn_schema)

    bad_conn = {"connections": [{"kind": "local_folder"}]}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad_conn, schema=conn_schema)

    # 2. Valid handoff payload validates against returned schema
    handoff_output = await tool.run(CTX, ProposalShapeArgs(kind=HANDOFF_KIND))
    assert handoff_output.is_error is False
    handoff_schema = json.loads(handoff_output.content)
    valid_handoff = handoff_payload("http://localhost:5000")
    assert HandoffPayload.model_validate(valid_handoff)
    jsonschema.validate(instance=valid_handoff, schema=handoff_schema)

    bad_handoff = {"models": "not-a-list"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad_handoff, schema=handoff_schema)


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
    defs = schema["$defs"]

    # 1. On-call rule: empty list [] when nobody is known
    on_call_desc = defs["Operations"]["properties"]["on_call"]["description"]
    assert "[]" in on_call_desc
    assert "nobody is known" in on_call_desc.lower()

    # 2. Placeholders rule: drawn from Placeholder constant
    placeholders_desc = defs["CommandSpec"]["properties"]["placeholders"]["description"]
    for placeholder in get_args(Placeholder):
        assert placeholder in placeholders_desc

    template_desc = defs["CommandSpec"]["properties"]["template"]["description"]
    for placeholder in get_args(Placeholder):
        assert f"{{{placeholder}}}" in template_desc


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
    proposing_defns = [defn for defn in catalog.all() if ProposeTool.name in defn.tools]
    assert len(proposing_defns) == 5
    for defn in proposing_defns:
        assert PROPOSAL_SHAPE in defn.tools, f"{defn.name} missing {PROPOSAL_SHAPE} in tools"
        low = defn.prompt.lower()
        assert "proposalshape" in low, f"{defn.name} prompt does not mention ProposalShape"


def test_proposal_shape_registered_in_default_registry(kinds_registry: ProposalKinds) -> None:
    registry = default_registry(proposal_kinds=kinds_registry)
    assert PROPOSAL_SHAPE in registry.names()
    tool = registry.get(PROPOSAL_SHAPE)
    assert tool is not None
    assert isinstance(tool, ProposalShapeTool)
