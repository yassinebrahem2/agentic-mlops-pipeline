"""Inbound delivery for messages between agents (I12)."""

from __future__ import annotations

from typing import Protocol

from langchain_core.messages import BaseMessage, HumanMessage

from mlops_agents.agents.runtime.contract import TextPart, TurnEnd
from mlops_agents.agents.runtime.hooks import InboundSource, TurnObserver
from mlops_agents.agents.runtime.hub import AsyncRLock, SessionHub
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.storage.databases import Databases


class QueuedTurns(Protocol):
    """What the inbound source needs of the session manager: each session's lock and hub (their
    one owner is the session registry), and the one owner that starts a worker for a session's
    queued turns."""

    def session_lock(self, project_id: str, session_id: str) -> AsyncRLock:
        """The session's lock, shared with Send, `get`, Stop and the worker."""
        ...

    def session_hub(self, project_id: str, session_id: str) -> SessionHub:
        """The session's hub, whose events reach the session's live stream."""
        ...

    async def start_queued_turns(self, project_id: str, session_id: str, agent: str) -> None:
        """Start a worker for the session's queued turns when it has some and none is running.
        The caller holds the session lock, or the session's worker is running."""
        ...


class SqliteInboundSource(InboundSource, TurnObserver):
    """Takes queued messages from other agents mid-turn, and re-delivers them when a turn does
    not end done."""

    def __init__(self, databases: Databases, store: SessionStore | None = None) -> None:
        self._databases = databases
        self._store = store or SessionStore(databases)
        self._sessions: QueuedTurns | None = None

    def bind(self, sessions: QueuedTurns) -> None:
        """The session manager binds itself; a second call raises RuntimeError."""
        if self._sessions is not None:
            raise RuntimeError("The inbound source is already bound.")
        self._sessions = sessions

    def _bound(self) -> QueuedTurns:
        """The session manager; an unbound source has no lock or hub, so it raises."""
        if self._sessions is None:
            raise RuntimeError("The inbound source is not bound to the session manager.")
        return self._sessions

    async def take(self, project_id: str, session_id: str, turn_id: str) -> list[BaseMessage]:
        """Select queued messages with from_agent set, mark each complete with read_by_turn
        set to current turn, delete their queued turns, publish message.updated, and return
        as HumanMessages with 'Message from <name>:' prefix."""
        sessions = self._bound()
        hub = sessions.session_hub(project_id, session_id)
        async with sessions.session_lock(project_id, session_id):
            updated = await self._store.take_inbound(project_id, session_id, turn_id)
            for msg in updated:
                await hub.message(msg, created=False)

            base_messages: list[BaseMessage] = []
            for msg in updated:
                user_text = "".join(p.text for p in msg.parts if isinstance(p, TextPart))
                sender = msg.from_agent or msg.author_name
                content = f"Message from {sender}:\n{user_text}"
                base_messages.append(HumanMessage(content=content))
            return base_messages

    async def turn_started(self, project_id: str, agent: str, turn_id: str) -> None:
        pass

    async def turn_waiting(self, project_id: str, agent: str, turn_id: str) -> None:
        pass

    async def turn_ended(
        self,
        project_id: str,
        agent: str,
        turn_id: str,
        status: TurnEnd,
        final_text: str | None,
    ) -> None:
        """When a turn does not end done (failed, stopped, interrupted), re-deliver messages
        taken mid-turn by creating new queued turns for them."""
        sessions = self._bound()
        if status == "done":
            return

        session_id = await self._store.session_id(project_id, agent)
        hub = sessions.session_hub(project_id, session_id)
        async with sessions.session_lock(project_id, session_id):
            redelivered = await self._store.redeliver_inbound(project_id, session_id, turn_id)
            for msg in redelivered:
                await hub.message(msg, created=False)

            if redelivered:
                await sessions.start_queued_turns(project_id, session_id, agent)
