"""Messages between agents (I12)."""

from __future__ import annotations

from typing import Protocol

from mlops_agents.agents.runtime.contract import AgentSessions


class MessageLimitReached(Exception):
    """Its message is written for the model."""


class MessageBus(Protocol):
    async def send(
        self, project_id: str, *, sender: str, sender_turn_id: str, to: str, text: str
    ) -> None:
        """SendMessage's whole behavior: can_message, exchange and hop, limits, post, event."""
        ...

    def bind(self, sessions: AgentSessions) -> None:
        """The session manager binds itself once, in its constructor; a second call raises
        RuntimeError. The bus is built first because the manager needs it."""
        ...
