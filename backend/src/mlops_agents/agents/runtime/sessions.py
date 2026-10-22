from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx2

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.agent import AgentBuilder
from mlops_agents.agents.runtime.contract import (
    AgentSessions,
    MessageStatus,
    MessageView,
    SessionView,
    StreamEvent,
    TextPart,
    TurnEnd,
)
from mlops_agents.agents.runtime.executor import TurnExecutor
from mlops_agents.agents.runtime.hub import AsyncRLock, SessionHub
from mlops_agents.agents.runtime.model_caller import ModelCaller
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools import ToolRegistry
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
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
        git: GitRunner,
    ) -> None:
        self._databases = databases
        self._workspace = workspace
        self._catalog = catalog
        self._tools = tools
        self._settings = settings
        self._providers = providers
        self._http = http
        self._git = git

        self._store = SessionStore(databases)
        self._caller = ModelCaller(providers=providers, http=http)
        self._builder = AgentBuilder(
            workspace=workspace,
            catalog=catalog,
            tools=tools,
            settings=settings,
            git=git,
        )
        self._executor = TurnExecutor(store=self._store, caller=self._caller, builder=self._builder)

        self._hubs: dict[tuple[str, str], SessionHub] = {}
        self._locks: dict[tuple[str, str], AsyncRLock] = {}
        self._workers: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._child_tasks: dict[tuple[str, str], asyncio.Task[tuple[TurnEnd, str | None]]] = {}
        self._stopped_tasks: set[asyncio.Task[Any]] = set()
        self._recovered_projects: set[str] = set()
        self._recovery_lock = asyncio.Lock()
        self._session_ids: dict[tuple[str, str], str] = {}
        self._session_id_lock = asyncio.Lock()

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
        key = (project_id, session_id)
        lock = self._get_lock(project_id, session_id)
        hub = self._get_hub(project_id, session_id)

        async with lock:
            worker = self._workers.get(key)
            is_busy = worker is not None and not worker.done()
            if not is_busy:
                queued = await self._store.next_queued_turn(project_id, session_id)
                if queued is not None:
                    self._workers[key] = asyncio.create_task(
                        self._worker_loop(project_id, session_id, agent)
                    )
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
        )

    async def send(self, project_id: str, agent: str, text: str) -> MessageView:
        await self._workspace.get(project_id)
        if not text.strip():
            raise DomainError("message_empty", "Message cannot be empty.", status=422)

        await self._ensure_recovered(project_id)
        self._check_agent(agent)

        session_id = await self._get_session_id(project_id, agent)
        key = (project_id, session_id)
        lock = self._get_lock(project_id, session_id)
        hub = self._get_hub(project_id, session_id)

        async with lock:
            worker = self._workers.get(key)
            is_busy = worker is not None and not worker.done()
            status: MessageStatus = "queued" if is_busy else "complete"

            user_msg = await self._store.add_message(
                project_id,
                session_id,
                author_kind="user",
                author_name="user",
                parts=[TextPart(type="text", text=text)],
                status=status,
            )
            await self._store.add_turn(project_id, session_id, user_msg.id, "queued")
            await hub.message(user_msg, created=True)

            if not is_busy:
                self._workers[key] = asyncio.create_task(
                    self._worker_loop(project_id, session_id, agent)
                )

        return user_msg

    async def _worker_loop(self, project_id: str, session_id: str, agent: str) -> None:
        key = (project_id, session_id)
        lock = self._get_lock(project_id, session_id)
        hub = self._get_hub(project_id, session_id)

        try:
            while True:
                async with lock:
                    next_turn = await self._store.next_queued_turn(project_id, session_id)
                    if next_turn is None:
                        self._workers.pop(key, None)
                        break

                turn_id, trigger_msg_id = next_turn
                await self._store.set_turn(project_id, turn_id, "running")

                # If trigger message was queued, update it to complete
                async with lock:
                    history = await self._store.messages(project_id, session_id)
                    triggers = [m for m in history if m.id == trigger_msg_id]
                    if triggers and triggers[0].status == "queued":
                        updated_msg = await self._store.update_message(
                            project_id, trigger_msg_id, status="complete"
                        )
                        await hub.message(updated_msg, created=False)

                child_task = asyncio.create_task(
                    self._executor.run(project_id, session_id, agent, turn_id, hub, lock=lock)
                )
                self._child_tasks[key] = child_task

                try:
                    try:
                        end_status, error = await child_task
                        await self._store.set_turn(project_id, turn_id, end_status, error=error)
                        await hub.turn_ended(turn_id, status=end_status, error=error)
                    except asyncio.CancelledError:
                        current = asyncio.current_task()
                        if current is not None and current.cancelling() > 0:
                            # The worker itself is being cancelled (close / close_project).
                            # Do not swallow worker cancellation!
                            if not child_task.done():
                                child_task.cancel()
                                with contextlib.suppress(asyncio.CancelledError, Exception):
                                    await child_task
                            await self._store.set_turn(project_id, turn_id, "interrupted")
                            await self._store.mark_interrupted(project_id, turn_id)
                            raise
                        if child_task in self._stopped_tasks:
                            self._stopped_tasks.discard(child_task)
                            await self._store.set_turn(project_id, turn_id, "stopped")
                            await hub.turn_ended(turn_id, status="stopped", error=None)
                            continue
                        await self._store.set_turn(project_id, turn_id, "interrupted")
                        await self._store.mark_interrupted(project_id, turn_id)
                        raise
                    except Exception as exc:
                        logger.exception("Unexpected error executing turn %s: %s", turn_id, exc)
                        err_text = str(exc) or type(exc).__name__
                        await self._store.set_turn(project_id, turn_id, "failed", error=err_text)
                        await hub.turn_ended(turn_id, status="failed", error=err_text)
                finally:
                    self._stopped_tasks.discard(child_task)
                    self._child_tasks.pop(key, None)

        finally:
            self._workers.pop(key, None)

    async def stop(self, project_id: str, agent: str) -> None:
        await self._workspace.get(project_id)
        self._check_agent(agent)
        session_id = await self._get_session_id(project_id, agent)
        key = (project_id, session_id)
        worker = self._workers.get(key)
        if worker is not None and not worker.done():
            child = self._child_tasks.get(key)
            if child is not None and not child.done():
                self._stopped_tasks.add(child)
                child.cancel()

    async def _subscribe_stream(
        self, project_id: str, agent: str, after_seq: int | None = None
    ) -> AsyncIterator[StreamEvent]:
        await self._workspace.get(project_id)
        self._check_agent(agent)
        session_id = await self._get_session_id(project_id, agent)
        hub = self._get_hub(project_id, session_id)
        async for event in hub.subscribe(after_seq):
            yield event

    def subscribe(
        self, project_id: str, agent: str, after_seq: int | None = None
    ) -> AsyncIterator[StreamEvent]:
        return self._subscribe_stream(project_id, agent, after_seq)

    async def close_project(self, project_id: str) -> None:
        await self._workspace.get(project_id)
        # Cancel workers for this project
        project_workers = [task for (p, _), task in list(self._workers.items()) if p == project_id]
        for task in project_workers:
            task.cancel()
        if project_workers:
            await asyncio.gather(*project_workers, return_exceptions=True)

        project_children = [
            task for (p, _), task in list(self._child_tasks.items()) if p == project_id
        ]
        for task in project_children:
            task.cancel()
        if project_children:
            await asyncio.gather(*project_children, return_exceptions=True)

        # Close hubs for this project
        for (p, s), hub in list(self._hubs.items()):
            if p == project_id:
                hub.close()
                self._hubs.pop((p, s), None)

        # Clear references
        for key in list(self._locks.keys()):
            if key[0] == project_id:
                self._locks.pop(key, None)
        for key in list(self._workers.keys()):
            if key[0] == project_id:
                self._workers.pop(key, None)
        for key in list(self._child_tasks.keys()):
            if key[0] == project_id:
                self._child_tasks.pop(key, None)
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
        projects = (
            {p for (p, _) in self._workers}
            | {p for (p, _) in self._child_tasks}
            | set(self._recovered_projects)
        )

        all_workers = list(self._workers.values())
        for task in all_workers:
            task.cancel()
        if all_workers:
            await asyncio.gather(*all_workers, return_exceptions=True)

        all_children = list(self._child_tasks.values())
        for task in all_children:
            task.cancel()
        if all_children:
            await asyncio.gather(*all_children, return_exceptions=True)

        for project_id in projects:
            try:
                await self._store.recover(project_id)
            except Exception as exc:
                logger.exception("Failed to recover project %s on close: %s", project_id, exc)

        for hub in list(self._hubs.values()):
            hub.close()

        self._hubs.clear()
        self._workers.clear()
        self._child_tasks.clear()
        self._stopped_tasks.clear()
        self._locks.clear()
        self._recovered_projects.clear()
        self._session_ids.clear()
