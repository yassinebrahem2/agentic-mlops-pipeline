"""Inbound delivery for messages between agents (I12)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from langchain_core.messages import BaseMessage, HumanMessage

from mlops_agents.agents.runtime.contract import TextPart, TurnEnd
from mlops_agents.agents.runtime.hooks import InboundSource, TurnObserver
from mlops_agents.agents.runtime.hub import AsyncRLock, SessionHub
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.storage.databases import Databases

if TYPE_CHECKING:
    from mlops_agents.agents.runtime.sessions import SessionManager
    from mlops_agents.agents.runtime.worker import TurnWorkers


class SqliteInboundSource(InboundSource, TurnObserver):
    """Takes queued messages from other agents mid-turn, and re-delivers them when a turn does
    not end done."""

    def __init__(self, databases: Databases, store: SessionStore | None = None) -> None:
        self._databases = databases
        self._store = store or SessionStore(databases)
        self._sessions: SessionManager | None = None
        self._locks: dict[tuple[str, str], AsyncRLock] = {}
        self._hubs: dict[tuple[str, str], SessionHub] = {}

    def bind(self, sessions: SessionManager) -> None:
        """The session manager binds itself; a second call raises RuntimeError."""
        if self._sessions is not None:
            raise RuntimeError("The inbound source is already bound.")
        self._sessions = sessions

    def _get_lock(self, project_id: str, session_id: str) -> AsyncRLock:
        if self._sessions is not None:
            fn = cast(
                "Callable[[str, str], AsyncRLock] | None",
                getattr(self._sessions, "_get_lock", None),
            )
            if fn is not None:
                return fn(project_id, session_id)
        key = (project_id, session_id)
        if key not in self._locks:
            self._locks[key] = AsyncRLock()
        return self._locks[key]

    def _get_hub(self, project_id: str, session_id: str) -> SessionHub:
        if self._sessions is not None:
            fn = cast(
                "Callable[[str, str], SessionHub] | None",
                getattr(self._sessions, "_get_hub", None),
            )
            if fn is not None:
                return fn(project_id, session_id)
        key = (project_id, session_id)
        if key not in self._hubs:
            self._hubs[key] = SessionHub(session_id, lock=self._get_lock(project_id, session_id))
        return self._hubs[key]

    async def take(self, project_id: str, session_id: str, turn_id: str) -> list[BaseMessage]:
        """Select queued messages with from_agent set, mark each complete with read_by_turn
        set to current turn, delete their queued turns, publish message.updated, and return
        as HumanMessages with 'Message from <name>:' prefix."""
        lock = self._get_lock(project_id, session_id)
        hub = self._get_hub(project_id, session_id)
        async with lock:
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
        if status == "done":
            return

        session_id = await self._store.session_id(project_id, agent)
        lock = self._get_lock(project_id, session_id)
        hub = self._get_hub(project_id, session_id)
        async with lock:
            redelivered = await self._store.redeliver_inbound(project_id, session_id, turn_id)
            for msg in redelivered:
                await hub.message(msg, created=False)

        if self._sessions is not None and redelivered:
            workers = cast("TurnWorkers | None", getattr(self._sessions, "_workers", None))
            if workers is not None and not workers.running((project_id, session_id)):
                workers.start(project_id, session_id, agent, hub, lock)
