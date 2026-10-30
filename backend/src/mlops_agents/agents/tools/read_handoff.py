"""ReadHandoff tool: read the project's approved onboarding handoff (I20)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.tools.base import (
    AgentTool,
    RiskClass,
    ToolContext,
    ToolOutput,
)
from mlops_agents.proposals.contract import HandoffPayload, HandoffView
from mlops_agents.proposals.payloads import Operations

if TYPE_CHECKING:
    from mlops_agents.proposals.contract import Handoffs

READ_HANDOFF_SECTION_CAP: Final[int] = 8000

SectionName = Literal[
    "models",
    "commands",
    "data_roles",
    "tracking",
    "pipeline",
    "edges",
    "operations",
    "gaps",
    "grants",
]


class ReadHandoffInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: SectionName | None = Field(
        default=None,
        description="The section of the handoff to read. Omit to list sections and their sizes.",
    )


class ReadHandoffTool(AgentTool[ReadHandoffInput]):
    name: ClassVar[str] = "ReadHandoff"
    description: ClassVar[str] = (
        "Read the current approved handoff produced by Onboarding, by section. "
        "Omit section to list available sections with item counts and sizes."
    )
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = ReadHandoffInput

    def __init__(self, handoffs: Handoffs | None = None) -> None:
        self._handoffs = handoffs

    async def run(self, ctx: ToolContext, args: ReadHandoffInput) -> ToolOutput:
        if self._handoffs is None:
            return ToolOutput("Onboarding has not handed off.")

        view: HandoffView | None = await self._handoffs.current(ctx.project_id)
        if view is None:
            return ToolOutput("Onboarding has not handed off.")

        payload = view.payload

        if args.section is None:
            return ToolOutput(self._format_index(payload))

        if args.section == "operations":
            return ToolOutput(self._format_operations(payload.operations))

        if args.section == "grants":
            content = payload.grants.model_dump_json(indent=2)
            if len(content) > READ_HANDOFF_SECTION_CAP:
                content = content[:READ_HANDOFF_SECTION_CAP] + "\n... (cut at the cap)"
            return ToolOutput(content)

        items = getattr(payload, args.section)
        return ToolOutput(self._format_list(items))

    def _format_index(self, payload: HandoffPayload) -> str:
        lines = ["Handoff sections:"]
        for field in HandoffPayload.model_fields:
            if field == "operations":
                ops_text = self._format_operations(payload.operations)
                count = len(Operations.model_fields)
                lines.append(f"- operations: {count} sub-keys ({len(ops_text)} characters)")
            elif field == "grants":
                grants_text = payload.grants.model_dump_json(indent=2)
                lines.append(f"- grants: 1 item ({len(grants_text)} characters)")
            else:
                items = getattr(payload, field)
                list_text = self._format_list(items)
                lines.append(f"- {field}: {len(items)} items ({len(list_text)} characters)")
        return "\n".join(lines)

    def _format_list(self, items: list[Any]) -> str:
        if not items:
            return "[]"

        # Check if first item alone exceeds cap
        first_json = (
            items[0].model_dump_json(indent=2)
            if hasattr(items[0], "model_dump_json")
            else json.dumps(items[0], indent=2)
        )
        if len(first_json) > READ_HANDOFF_SECTION_CAP:
            omitted = len(items) - 1
            omitted_msg = f"; {omitted} more items omitted" if omitted > 0 else ""
            cut_item = first_json[:READ_HANDOFF_SECTION_CAP]
            return f"[\n{cut_item}\n... (cut at the cap{omitted_msg})\n]"

        # Collect items that fit under cap
        kept: list[dict[str, Any]] = []
        for item in items:
            item_data = item.model_dump() if hasattr(item, "model_dump") else item
            test_list = [*kept, item_data]
            test_json = json.dumps(test_list, indent=2)
            if len(test_json) > READ_HANDOFF_SECTION_CAP and kept:
                omitted = len(items) - len(kept)
                base_json = json.dumps(kept, indent=2)
                return f"{base_json}\n({omitted} more items omitted)"
            kept.append(item_data)

        return json.dumps(kept, indent=2)

    def _format_operations(self, operations: Operations) -> str:
        sections: list[str] = []
        for key in Operations.model_fields:
            val = getattr(operations, key)
            if isinstance(val, list):
                val_text = self._format_list(cast(list[Any], val))
            elif hasattr(val, "model_dump_json"):
                val_text = val.model_dump_json(indent=2)
                if len(val_text) > READ_HANDOFF_SECTION_CAP:
                    val_text = val_text[:READ_HANDOFF_SECTION_CAP] + "\n... (cut at the cap)"
            elif val is None:
                val_text = "null"
            else:
                val_text = json.dumps(val, indent=2)
                if len(val_text) > READ_HANDOFF_SECTION_CAP:
                    val_text = val_text[:READ_HANDOFF_SECTION_CAP] + "\n... (cut at the cap)"
            sections.append(f"{key}:\n{val_text}")
        return "\n\n".join(sections)
