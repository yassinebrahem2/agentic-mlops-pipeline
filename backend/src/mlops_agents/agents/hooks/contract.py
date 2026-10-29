"""Context hooks (I18): text rendered into an agent's context when a turn starts or resumes."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class HookContext(BaseModel):
    project_id: str
    agent: str
    trigger_text: str


class ContextHook(Protocol):
    name: str

    async def render(self, ctx: HookContext) -> str:
        """Capped text. Never raises: a failure renders as one line."""
        ...
