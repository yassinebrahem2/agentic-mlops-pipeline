"""SearchNotes tool (Task 5)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.tools.base import (
    AgentTool,
    RiskClass,
    ToolContext,
    ToolOutput,
)
from mlops_agents.knowledge.contract import NoteType
from mlops_agents.knowledge.limits import SEARCH_MAX_LIMIT

if TYPE_CHECKING:
    from mlops_agents.knowledge.contract import Knowledge

MAX_OUTPUT_CHARS = 12_000
TEXT_PREVIEW_CHARS = 500


class SearchNotesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(description="Search text or question to match.")
    type: NoteType | None = Field(default=None, description="Optional note type filter.")
    limit: int = Field(
        default=5,
        ge=1,
        le=SEARCH_MAX_LIMIT,
        description=f"Maximum number of notes to return (default 5, max {SEARCH_MAX_LIMIT}).",
    )


class SearchNotes(AgentTool[SearchNotesArgs]):
    name: ClassVar[str] = "SearchNotes"
    description: ClassVar[str] = (
        "Search the project knowledge base by keywords and meaning. "
        "Returns relevant notes ordered by relevance."
    )
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = SearchNotesArgs

    def __init__(self, knowledge: Knowledge) -> None:
        self._knowledge = knowledge

    async def run(self, ctx: ToolContext, args: SearchNotesArgs) -> ToolOutput:
        result = await self._knowledge.search(
            ctx.project_id,
            query=args.query,
            type=args.type,
            limit=args.limit,
        )

        detail_line = (
            f"\nNote: {result.detail}" if (not result.used_vectors and result.detail) else ""
        )

        if not result.notes:
            return ToolOutput(f"No notes match.{detail_line}")

        blocks = [
            f"[{note.type}] {note.title} ({note.id})\n"
            f"Source: {note.source or 'none'}\n"
            f"{note.text[:TEXT_PREVIEW_CHARS]}"
            for note in result.notes
        ]

        full_text = "\n\n".join(blocks) + detail_line
        if len(full_text) <= MAX_OUTPUT_CHARS:
            return ToolOutput(full_text)

        # Truncate notes to fit MAX_OUTPUT_CHARS
        for k in range(len(blocks) - 1, 0, -1):
            omitted = len(blocks) - k
            omitted_msg = f"\n({omitted} note{'s' if omitted != 1 else ''} omitted due to length)"
            candidate = "\n\n".join(blocks[:k]) + omitted_msg + detail_line
            if len(candidate) <= MAX_OUTPUT_CHARS:
                return ToolOutput(candidate)

        omitted = len(blocks)
        omitted_msg = f"({omitted} note{'s' if omitted != 1 else ''} omitted due to length)"
        return ToolOutput((omitted_msg + detail_line)[:MAX_OUTPUT_CHARS])
