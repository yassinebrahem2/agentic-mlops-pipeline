"""Test double for TurnEndCheck."""

from __future__ import annotations


class NoTurnEndCheck:
    """Stage 1 test double: a turn ends when the model ends it, with no reminder note."""

    async def note(self, project_id: str, session_id: str, turn_id: str) -> str | None:
        return None
