"""SaveNote tool (Task 5)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.tools.base import (
    AgentTool,
    RiskClass,
    ToolContext,
    ToolFailure,
    ToolOutput,
)
from mlops_agents.errors import DomainError
from mlops_agents.knowledge.contract import NoteDraft, NoteType

if TYPE_CHECKING:
    from mlops_agents.knowledge.contract import Knowledge


class SaveNoteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: NoteType = Field(
        description="Type of the note: fact, decision, incident, runbook, or doc."
    )
    title: str = Field(description="Short title describing the note.")
    text: str = Field(description="Full text content of the note.")
    source: str | None = Field(default=None, description="Optional source reference or link.")
    note_id: str | None = Field(
        default=None,
        description="ID of an existing note to update (e.g. kno_...).",
    )


class SaveNote(AgentTool[SaveNoteArgs]):
    name: ClassVar[str] = "SaveNote"
    description: ClassVar[str] = (
        "Save or update a note in the project's knowledge base. Use for facts, decisions, "
        "incidents, runbooks, and team documents. Do not use for code (use file tools), "
        "secrets, or large data tables. To update an existing note, pass note_id from a search."
    )
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = SaveNoteArgs

    def __init__(self, knowledge: Knowledge) -> None:
        self._knowledge = knowledge

    async def run(self, ctx: ToolContext, args: SaveNoteArgs) -> ToolOutput:
        try:
            note = await self._knowledge.save(
                ctx.project_id,
                author=ctx.agent,
                note=NoteDraft(
                    type=args.type,
                    title=args.title,
                    text=args.text,
                    source=args.source,
                ),
                note_id=args.note_id,
            )
        except DomainError as exc:
            raise ToolFailure(exc.message) from exc

        if args.note_id is None:
            return ToolOutput(f"Saved note {note.id} ({note.type}): {note.title}")
        return ToolOutput(f"Updated note {note.id} ({note.type}): {note.title}")
