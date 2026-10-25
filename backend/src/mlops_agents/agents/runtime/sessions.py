from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import aclosing
from typing import Literal

import httpx2
from sqlalchemy import select

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.messaging.contract import MessageBus
from mlops_agents.agents.runtime.agent import AgentBuilder
from mlops_agents.agents.runtime.contract import (
    AgentSessions,
    MessageStatus,
    MessageView,
    Part,
    SessionView,
    StreamEvent,
    TextPart,
)
from mlops_agents.agents.runtime.executor import TurnExecutor
from mlops_agents.agents.runtime.graph import TurnHooks
from mlops_agents.agents.runtime.hooks import Gate, InboundSource, TurnEndCheck, TurnObserver
from mlops_agents.agents.runtime.hub import AsyncRLock, SessionHub
from mlops_agents.agents.runtime.model_caller import ModelCaller
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.runtime.worker import TurnWorkers
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tasks.contract import TaskWorkspaces
from mlops_agents.agents.tools import ToolRegistry
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import StatusBoard
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import sessions as sessions_table
from mlops_agents.storage.tables import turns as turns_table
from mlops_agents.workspace.service import WorkspaceService

logger = logging.getLogger(__name__)

__all__ = ["SessionManager"]


class SessionManager(AgentSessions):
    """Manages agent chat sessions, turns, worker tasks, and event streams."""

    def __init__(
        self,
        *,
        databases: Databases,
        workspace: WorkspaceService,
        catalog: AgentCatalog,
        tools: ToolRegistry,
        settings: AgentSettingsService,
        providers: ProvidersService,
        http: httpx2.AsyncClient,
        gate: Gate,
        inbound: InboundSource,
        turn_end: TurnEndCheck,
        workspaces: TaskWorkspaces,
        observers: Sequence[TurnObserver] = (),
        bus: MessageBus | None = None,
        status_board: StatusBoard | None = None,
    ) -> None:
        self._databases = databases
        self._workspace = workspace
        self._catalog = catalog
        self._tools = tools
        self._settings = settings
        self._providers = providers
        self._http = http

        self._store = SessionStore(databases)
        self._caller = ModelCaller(providers=providers, http=http)
        self._builder = AgentBuilder(
            workspace=workspace,
            catalog=catalog,
            tools=tools,
            settings=settings,
            workspaces=workspaces,
            status_board=status_board,
        )
        self._executor = TurnExecutor(
            store=self._store,
            caller=self._caller,
            builder=self._builder,
            hooks=TurnHooks(gate=gate, inbound=inbound, turn_end=turn_end, workspaces=workspaces),
        )
        self._workers = TurnWorkers(store=self._store, executor=self._executor, observers=observers)

        self._hubs: dict[tuple[str, str], SessionHub] = {}
        self._locks: dict[tuple[str, str], AsyncRLock] = {}
        self._recovered_projects: set[str] = set()
        self._recovery_lock = asyncio.Lock()
        self._session_ids: dict[tuple[str, str], str] = {}
        self._session_id_lock = asyncio.Lock()

        # The bus is built first, and the manager binds itself to it once it is complete.
        if bus is not None:
            bus.bind(self)

    def _get_lock(self, project_id: str, session_id: str) -> AsyncRLock:
        key = (project_id, session_id)
        if key not in self._locks:
            self._locks[key] = AsyncRLock()
        return self._locks[key]

    def _get_hub(self, project_id: str, session_id: str) -> SessionHub:
        key = (project_id, session_id)
        if key not in self._hubs:
            lock = self._get_lock(project_id, session_id)
            self._hubs[key] = SessionHub(session_id, lock=lock)
        return self._hubs[key]

    async def _ensure_recovered(self, project_id: str) -> None:
        if project_id in self._recovered_projects:
            return
        async with self._recovery_lock:
            if project_id in self._recovered_projects:
                return
            await self._store.recover(project_id)
            self._recovered_projects.add(project_id)

    async def _get_session_id(self, project_id: str, agent: str) -> str:
        key = (project_id, agent)
        if key in self._session_ids:
            return self._session_ids[key]
        async with self._session_id_lock:
            if key in self._session_ids:
                return self._session_ids[key]
            sid = await self._store.session_id(project_id, agent)
            self._session_ids[key] = sid
            return sid

    def _check_agent(self, agent: str) -> None:
        definition = self._catalog.get(agent)
        if not definition.chat:
            raise DomainError(
                "agent_cannot_chat",
                f"Agent '{agent}' does not accept chat messages.",
                status=409,
            )

    async def get(self, project_id: str, agent: str) -> SessionView:
        await self._workspace.get(project_id)
        await self._ensure_recovered(project_id)
        self._check_agent(agent)

        session_id = await self._get_session_id(project_id, agent)
        lock = self._get_lock(project_id, session_id)
        hub = self._get_hub(project_id, session_id)

        async with lock:
            is_busy = await self._busy(project_id, session_id)
            if not is_busy:
                queued = await self._store.next_queued_turn(project_id, session_id)
                if queued is not None:
                    self._workers.start(project_id, session_id, agent, hub, lock)
                    is_busy = True

            stored = await self._store.messages(project_id, session_id)
            live_msg = hub.live_message
            if is_busy and live_msg is not None:
                messages_view = [live_msg if m.id == live_msg.id else m for m in stored]
            else:
                messages_view = stored

            last_seq = hub.last_seq

        return SessionView(
            id=session_id,
            agent=agent,
            busy=is_busy,
            messages=messages_view,
            last_seq=last_seq,
            current_task=None,
        )

    async def _busy(self, project_id: str, session_id: str) -> bool:
        """The one busy rule: a live worker, or a turn waiting for approval. Under the lock."""
        return self._workers.running(
            (project_id, session_id)
        ) or await self._store.has_waiting_turn(project_id, session_id)

    async def send(self, project_id: str, agent: str, text: str) -> MessageView:
        return await self._enqueue(
            project_id,
            agent,
            text,
            author_name="user",
            from_agent=None,
            exchange_id=None,
            hop=None,
        )

    async def post(
        self,
        project_id: str,
        agent: str,
        *,
        author_name: str,
        from_agent: str,
        text: str,
        exchange_id: str | None,
        hop: int | None,
    ) -> MessageView:
        return await self._enqueue(
            project_id,
            agent,
            text,
            author_name=author_name,
            from_agent=from_agent,
            exchange_id=exchange_id,
            hop=hop,
        )

    async def _enqueue(
        self,
        project_id: str,
        agent: str,
        text: str,
        *,
        author_name: str,
        from_agent: str | None,
        exchange_id: str | None,
        hop: int | None,
    ) -> MessageView:
        """Store an input message and its queued turn; start the worker when the session is idle."""
        await self._workspace.get(project_id)
        if not text.strip():
            raise DomainError("message_empty", "Message cannot be empty.", status=422)

        await self._ensure_recovered(project_id)
        self._check_agent(agent)

        session_id = await self._get_session_id(project_id, agent)
        lock = self._get_lock(project_id, session_id)
        hub = self._get_hub(project_id, session_id)

        async with lock:
            is_busy = await self._busy(project_id, session_id)
            status: MessageStatus = "queued" if is_busy else "complete"

            message = await self._store.add_message(
                project_id,
                session_id,
                author_kind="user",
                author_name=author_name,
                parts=[TextPart(type="text", text=text)],
                status=status,
                from_agent=from_agent,
                exchange_id=exchange_id,
                hop=hop,
            )
            await self._store.add_turn(project_id, session_id, message.id, "queued")
            await hub.message(message, created=True)

            if not is_busy:
                self._workers.start(project_id, session_id, agent, hub, lock)

        return message

    async def turn_states(self, project_id: str) -> dict[str, Literal["running", "waiting"]]:
        """Each agent whose session has a running or waiting turn now."""
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (await conn.execute(select(sessions_table.c.id, sessions_table.c.agent))).all()
            session_to_agent = {str(r[0]): str(r[1]) for r in rows}

            waiting_rows = (
                await conn.execute(
                    select(turns_table.c.session_id)
                    .where(turns_table.c.status == "waiting")
                    .distinct()
                )
            ).all()
            waiting_session_ids = {str(r[0]) for r in waiting_rows}

        running_session_ids = self._workers.running_sessions(project_id)
        states: dict[str, Literal["running", "waiting"]] = {}
        for sid, agent in session_to_agent.items():
            if sid in running_session_ids:
                states[agent] = "running"
            elif sid in waiting_session_ids:
                states[agent] = "waiting"
        return states

    async def update_message(
        self,
        project_id: str,
        session_id: str,
        message_id: str,
        transform: Callable[[list[Part]], list[Part]],
    ) -> MessageView:
        await self._workspace.get(project_id)
        # Both are checked before the session gets a lock or a hub, so a bad id keeps no state.
        if not await self._store.has_session(project_id, session_id):
            raise DomainError("session_not_found", "This session does not exist.", status=404)
        await self._message(project_id, session_id, message_id)
        lock = self._get_lock(project_id, session_id)
        hub = self._get_hub(project_id, session_id)
        # Read, write and publish are one step under the session lock: a concurrent get() never
        # returns parts whose event it then receives again, and concurrent updates all land.
        async with lock:
            current = await self._message(project_id, session_id, message_id)
            updated = await self._store.update_message(
                project_id, message_id, parts=transform(list(current.parts))
            )
            await hub.message(updated, created=False)
        return updated

    async def _message(self, project_id: str, session_id: str, message_id: str) -> MessageView:
        message = await self._store.message(project_id, session_id, message_id)
        if message is None:
            raise DomainError("message_not_found", "This message does not exist.", status=404)
        return message

    async def stop(self, project_id: str, agent: str) -> None:
        await self._workspace.get(project_id)
        self._check_agent(agent)
        session_id = await self._get_session_id(project_id, agent)
        self._workers.stop((project_id, session_id))

    async def _subscribe_stream(
        self, project_id: str, agent: str, after_seq: int | None = None
    ) -> AsyncGenerator[StreamEvent, None]:
        await self._workspace.get(project_id)
        self._check_agent(agent)
        session_id = await self._get_session_id(project_id, agent)
        hub = self._get_hub(project_id, session_id)
        # The hub drops its subscriber in the generator's finally, which runs when the
        # generator is closed, not when it is garbage collected.
        async with aclosing(hub.subscribe(after_seq)) as events:
            async for event in events:
                yield event

    def subscribe(
        self, project_id: str, agent: str, after_seq: int | None = None
    ) -> AsyncGenerator[StreamEvent, None]:
        return self._subscribe_stream(project_id, agent, after_seq)

    async def close_project(self, project_id: str) -> None:
        await self._workspace.get(project_id)
        await self._workers.cancel(project_id)

        # Close hubs for this project
        for (p, s), hub in list(self._hubs.items()):
            if p == project_id:
                hub.close()
                self._hubs.pop((p, s), None)

        # Clear references
        for key in list(self._locks.keys()):
            if key[0] == project_id:
                self._locks.pop(key, None)
        self._workers.forget(project_id)
        for key in list(self._session_ids.keys()):
            if key[0] == project_id:
                self._session_ids.pop(key, None)

        self._recovered_projects.discard(project_id)

        # Mark any interrupted turns or messages in DB
        try:
            await self._store.recover(project_id)
        except Exception as exc:
            logger.exception("Failed to recover project %s on close_project: %s", project_id, exc)

        # Close SQLite database connections so directory can be removed
        await self._databases.close_project(project_id)

    async def close(self) -> None:
        # Recover projects with running turns - capture projects BEFORE cancelling workers
        projects = self._workers.projects() | set(self._recovered_projects)

        await self._workers.cancel()

        for project_id in projects:
            try:
                await self._store.recover(project_id)
            except Exception as exc:
                logger.exception("Failed to recover project %s on close: %s", project_id, exc)

        for hub in list(self._hubs.values()):
            hub.close()

        self._hubs.clear()
        self._workers.forget()
        self._locks.clear()
        self._recovered_projects.clear()
        self._session_ids.clear()
