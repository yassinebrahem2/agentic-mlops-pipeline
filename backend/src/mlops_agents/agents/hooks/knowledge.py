"""Knowledge context hook (Task 5)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from mlops_agents.agents.hooks.contract import HookContext
from mlops_agents.errors import DomainError

if TYPE_CHECKING:
    from mlops_agents.knowledge.contract import Knowledge

MAX_HOOK_CHARS = 2500
MAX_HOOK_NOTES = 5
HOOK_TEXT_PREVIEW_CHARS = 300


class KnowledgeHook:
    name: str = "knowledge"

    def __init__(self, knowledge: Knowledge) -> None:
        self._knowledge = knowledge

    async def render(self, ctx: HookContext) -> str:
        if not ctx.trigger_text.strip():
            return ""

        try:
            result = await self._knowledge.search(
                ctx.project_id,
                query=ctx.trigger_text,
                type=None,
                limit=MAX_HOOK_NOTES,
            )
        except (DomainError, SQLAlchemyError) as exc:
            return f"Knowledge notes are unavailable: {exc}"

        if not result.notes:
            return ""

        lines = ["Project knowledge:"]
        for note in result.notes[:MAX_HOOK_NOTES]:
            lines.append(
                f"- [{note.type}] {note.title} ({note.id}): {note.text[:HOOK_TEXT_PREVIEW_CHARS]}"
            )

        text = "\n".join(lines)
        if len(text) > MAX_HOOK_CHARS:
            return text[:MAX_HOOK_CHARS]
        return text
