"""Each session's live objects in this process: its lock, its event hub and its stored id."""

from __future__ import annotations

import asyncio

from mlops_agents.agents.runtime.hub import AsyncRLock, SessionHub
from mlops_agents.agents.runtime.store import SessionStore

__all__ = ["SessionRegistry"]


class SessionRegistry:
    """Hands out one lock and one hub per session, and caches each agent's session id."""

    def __init__(self, store: SessionStore) -> None:
        self._store = store
        self._hubs: dict[tuple[str, str], SessionHub] = {}
        self._locks: dict[tuple[str, str], AsyncRLock] = {}
        self._session_ids: dict[tuple[str, str], str] = {}
        self._session_id_lock = asyncio.Lock()

    def lock(self, project_id: str, session_id: str) -> AsyncRLock:
        return self._locks.setdefault((project_id, session_id), AsyncRLock())

    def hub(self, project_id: str, session_id: str) -> SessionHub:
        key = (project_id, session_id)
        if key not in self._hubs:
            self._hubs[key] = SessionHub(session_id, lock=self.lock(project_id, session_id))
        return self._hubs[key]

    async def session_id(self, project_id: str, agent: str) -> str:
        key = (project_id, agent)
        if key in self._session_ids:
            return self._session_ids[key]
        async with self._session_id_lock:
            if key in self._session_ids:
                return self._session_ids[key]
            sid = await self._store.session_id(project_id, agent)
            self._session_ids[key] = sid
            return sid

    def forget(self, project_id: str) -> None:
        """Close the project's hubs and drop its locks and session ids."""
        for (p, s), hub in list(self._hubs.items()):
            if p == project_id:
                hub.close()
                self._hubs.pop((p, s), None)
        self._locks = {k: v for k, v in self._locks.items() if k[0] != project_id}
        self._session_ids = {k: v for k, v in self._session_ids.items() if k[0] != project_id}

    def clear(self) -> None:
        """Close every hub that is left, and drop every hub, lock and session id."""
        for hub in self._hubs.values():
            hub.close()
        self._hubs.clear()
        self._locks.clear()
        self._session_ids.clear()
