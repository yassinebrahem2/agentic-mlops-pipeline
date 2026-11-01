from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import aclosing
from typing import Literal

import httpx2

from mlops_agents.agents.approvals.contract import Approvals
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.hooks.registry import HookRegistry
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
from mlops_agents.agents.runtime.repair import ProjectRecovery
from mlops_agents.agents.runtime.session_registry import SessionRegistry
from mlops_agents.agents.runtime.stall import LateApprovals
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.runtime.waiting import stop_waiting_turn
from mlops_agents.agents.runtime.worker import TurnWorkers
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tasks.contract import TaskWorkspaces
from mlops_agents.agents.tools import ToolRegistry
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import AuditLog, EventLog
from mlops_agents.providers.failover import KeyFailover
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.workspace.service import WorkspaceService

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
        failover: KeyFailover,
        gate: Gate,
        inbound: InboundSource,
        turn_end: TurnEndCheck,
        workspaces: TaskWorkspaces,
        approval_store: ApprovalStore,
        observers: Sequence[TurnObserver] = (),
        bus: MessageBus | None = None,
        hooks: HookRegistry | None = None,
        audit_log: AuditLog | None = None,
        event_log: EventLog | None = None,
    ) -> None:
        self._databases = databases
        self._workspace = workspace
        self._catalog = catalog
        self._tools = tools
        self._settings = settings
        self._providers = providers
        self._http = http
        self._workspaces = workspaces
        self._audit_log = audit_log
        self._event_log = event_log
        self._observers = tuple(observers)
        self._approval_store = approval_store

        self._store = SessionStore(databases, audit_log=self._audit_log, event_log=self._event_log)
        self._caller = ModelCaller(providers=providers, http=http, failover=failover)
        self._approvals = LateApprovals()
        self._builder = AgentBuilder(
            workspace=workspace,
            catalog=catalog,
            tools=tools,
            settings=settings,
            workspaces=workspaces,
            hooks=hooks,
            store=self._store,
        )
        self._executor = TurnExecutor(
            store=self._store,
            caller=self._caller,
            builder=self._builder,
            hooks=TurnHooks(
                gate=gate,
                inbound=inbound,
                turn_end=turn_end,
                workspaces=workspaces,
                approval_store=approval_store,
                approvals=self._approvals,
                events=event_log,
            ),
            observers=observers,
        )
        self._workers = TurnWorkers(
            store=self._store,
            executor=self._executor,
            observers=observers,
            approval_store=approval_store,
            audit_log=audit_log,
            event_log=event_log,
        )

        self._registry = SessionRegistry(self._store)
        self._recovery = ProjectRecovery(
            store=self._store,
            approval_store=approval_store,
            databases=databases,
            audit_log=audit_log,
            event_log=event_log,
            rewrite_part=self._rewrite_part,
            stop_turn=self.stop_turn,
            resume_turn=self.resume_turn,
        )

        if bus is not None:
            bus.bind(self)

    def bind_approvals(self, approvals: Approvals) -> None:
        """The approvals service, which takes this manager and so is built after it; a turn's
        stall goes through it. A second call raises RuntimeError."""
        self._approvals.bind(approvals)

    async def ensure_recovered(self, project_id: str) -> None:
        """Public method to ensure a project's state is recovered before use."""
        await self._recovery.ensure(project_id)

    def _check_agent(self, agent: str) -> None:
        if not self._catalog.get(agent).chat:
            raise DomainError(
                "agent_cannot_chat", f"Agent '{agent}' does not accept chat messages.", status=409
            )

    async def get(self, project_id: str, agent: str) -> SessionView:
        await self._workspace.get(project_id)
        await self._recovery.ensure(project_id)
        self._check_agent(agent)

        session_id = await self._registry.session_id(project_id, agent)
        lock = self._registry.lock(project_id, session_id)
        hub = self._registry.hub(project_id, session_id)

        async with lock:
            is_busy = await self._busy(project_id, session_id)
            if not is_busy:
                await self.start_queued_turns(project_id, session_id, agent)
                is_busy = self._workers.running((project_id, session_id))

            stored = await self._store.messages(project_id, session_id)
            live = hub.live_message if is_busy else None
            messages = [live if (live and m.id == live.id) else m for m in stored]
            last_seq = hub.last_seq

        current_ref = await self._workspaces.current(project_id, session_id)
        current_task = (
            await self._workspaces.view(project_id, current_ref.id)
            if current_ref is not None
            else None
        )

        return SessionView(
            id=session_id,
            agent=agent,
            busy=is_busy,
            messages=messages,
            last_seq=last_seq,
            current_task=current_task,
        )

    async def _busy(self, project_id: str, session_id: str) -> bool:
        """The one busy rule: a live worker, or a turn waiting for approval. Under the lock."""
        return self._workers.running(
            (project_id, session_id)
        ) or await self._store.has_waiting_turn(project_id, session_id)

    async def send(self, project_id: str, agent: str, text: str) -> MessageView:
        return await self._enqueue(
            project_id, agent, text, author_name="user", from_agent=None, exchange_id=None, hop=None
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
        system: bool = False,
    ) -> MessageView:
        return await self._enqueue(
            project_id,
            agent,
            text,
            author_name=author_name,
            from_agent=from_agent,
            exchange_id=exchange_id,
            hop=hop,
            system=system,
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
        system: bool = False,
    ) -> MessageView:
        await self._workspace.get(project_id)
        if not text.strip():
            raise DomainError("message_empty", "Message cannot be empty.", status=422)

        await self._recovery.ensure(project_id)
        if not system:
            self._check_agent(agent)

        session_id = await self._registry.session_id(project_id, agent)
        lock = self._registry.lock(project_id, session_id)
        hub = self._registry.hub(project_id, session_id)

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
        session_to_agent, waiting_ids = await self._store.session_agent_map(project_id)
        running_ids = self._workers.running_sessions(project_id)
        return {
            agent: ("running" if sid in running_ids else "waiting")
            for sid, agent in session_to_agent.items()
            if sid in running_ids or sid in waiting_ids
        }

    async def update_message(
        self,
        project_id: str,
        session_id: str,
        message_id: str,
        transform: Callable[[list[Part]], list[Part]],
    ) -> MessageView:
        await self._workspace.get(project_id)
        if not await self._store.has_session(project_id, session_id):
            raise DomainError("session_not_found", "This session does not exist.", status=404)
        updated = await self._rewrite_part(project_id, session_id, message_id, transform)
        if updated is None:
            raise DomainError("message_not_found", "This message does not exist.", status=404)
        return updated

    async def _rewrite_part(
        self,
        project_id: str,
        session_id: str,
        message_id: str,
        transform: Callable[[list[Part]], list[Part]],
    ) -> MessageView | None:
        """Store and publish the message's parts, transformed, under the session lock; None if gone."""
        async with self._registry.lock(project_id, session_id):
            updated = await self._store.rewrite_parts(project_id, session_id, message_id, transform)
            if updated is not None:
                await self._registry.hub(project_id, session_id).message(updated, created=False)
        return updated

    async def resume_turn(self, project_id: str, session_id: str, turn_id: str) -> None:
        await self._workspace.get(project_id)
        await self._recovery.ensure(project_id)
        if not await self._store.has_session(project_id, session_id):
            raise DomainError("session_not_found", "This session does not exist.", status=404)

        agent = await self._store.session_agent(project_id, session_id)
        if agent is None:
            raise RuntimeError(f"Session {session_id} has no agent")

        worker = self._workers.get((project_id, session_id))
        if worker is not None and not worker.done():
            await worker

        lock = self._registry.lock(project_id, session_id)
        hub = self._registry.hub(project_id, session_id)

        async with lock:
            status = await self._store.turn_status(project_id, turn_id)
            if status != "waiting":
                return
            if self._workers.running((project_id, session_id)):
                return
            self._workers.resume(project_id, session_id, agent, turn_id, hub, lock)

    async def stop(self, project_id: str, agent: str) -> None:
        await self._workspace.get(project_id)
        self._check_agent(agent)
        await self._recovery.ensure(project_id)
        await self._stop(project_id, agent, None)

    async def stop_turn(self, project_id: str, session_id: str, turn_id: str) -> None:
        await self._workspace.get(project_id)
        agent = await self._store.session_agent(project_id, session_id)
        if agent is None:
            raise DomainError("session_not_found", "This session does not exist.", status=404)
        await self._recovery.ensure(project_id)
        await self._stop(project_id, agent, turn_id)

    async def _stop(self, project_id: str, agent: str, turn_id: str | None) -> None:
        """Stop the agent's turn, running or waiting; given `turn_id`, only that turn. A running
        worker is stopped at once, without the lock; a waiting turn is stopped under the session
        lock, where the resume's worker is started and takes it, so a Stop lands either before
        that start or on that worker (#446)."""
        session_id = await self._registry.session_id(project_id, agent)
        key = (project_id, session_id)
        if self._workers.stop(key, turn_id) in ("landed", "dropped"):
            return

        lock = self._registry.lock(project_id, session_id)
        hub = self._registry.hub(project_id, session_id)
        while True:
            async with lock:
                worker = self._workers.get(key)
                if worker is None or worker.done():
                    await self._stop_waiting(project_id, session_id, agent, turn_id, hub)
                    return
                if self._workers.stop(key, turn_id) != "waiting":
                    return
            # The worker whose turn went waiting needs the lock to finish: never under it.
            await worker

    async def _stop_waiting(
        self,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str | None,
        hub: SessionHub,
    ) -> None:
        """Stop the session's waiting turn (given `turn_id`, only that one) and start a worker for
        its queued turns, unless the stop's observers started one (a redelivery). Under the
        session lock, with no worker running."""
        waiting = await self._store.waiting_turn(project_id, session_id)
        if waiting is None or turn_id not in (None, waiting[0]):
            return
        await stop_waiting_turn(
            project_id=project_id,
            session_id=session_id,
            agent=agent,
            turn_id=waiting[0],
            store=self._store,
            hub=hub,
            executor=self._executor,
            approval_store=self._approval_store,
            audit_log=self._audit_log,
            event_log=self._event_log,
            observers=self._observers,
        )
        await self.start_queued_turns(project_id, session_id, agent)

    def session_lock(self, project_id: str, session_id: str) -> AsyncRLock:
        """The session's one lock, from the registry (the inbound source's `QueuedTurns`)."""
        return self._registry.lock(project_id, session_id)

    def session_hub(self, project_id: str, session_id: str) -> SessionHub:
        """The session's one hub, from the registry (the inbound source's `QueuedTurns`)."""
        return self._registry.hub(project_id, session_id)

    async def start_queued_turns(self, project_id: str, session_id: str, agent: str) -> None:
        """Start a worker for the session's queued turns when it has some and none is running.
        The caller holds the session lock, or the session's worker is running (then this returns
        before any await). The worker is started by a Stop, a redelivery or a `get` after a
        restart, not by the Send of the turn it takes, so a Stop before it takes that turn comes
        between two turns, and is dropped (`stops_first_turn=False`)."""
        if self._workers.running((project_id, session_id)):
            return
        if await self._store.next_queued_turn(project_id, session_id) is None:
            return
        self._workers.start(
            project_id,
            session_id,
            agent,
            self._registry.hub(project_id, session_id),
            self._registry.lock(project_id, session_id),
            stops_first_turn=False,
        )

    async def subscribe(
        self, project_id: str, agent: str, after_seq: int | None = None
    ) -> AsyncGenerator[StreamEvent, None]:
        await self._workspace.get(project_id)
        self._check_agent(agent)
        session_id = await self._registry.session_id(project_id, agent)
        hub = self._registry.hub(project_id, session_id)
        async with aclosing(hub.subscribe(after_seq)) as events:
            async for event in events:
                yield event

    async def _recover_and_clear_project(self, project_id: str) -> None:
        self._registry.forget(project_id)
        self._workers.forget(project_id)
        self._recovery.forget(project_id)
        await self._recovery.recover_only(project_id)

    async def close_project(self, project_id: str) -> None:
        await self._workspace.get(project_id)
        # The repair first: one about to resume a turn would start a worker after the cancel.
        await self._recovery.cancel(project_id)
        await self._workers.cancel(project_id)
        await self._recover_and_clear_project(project_id)
        await self._databases.close_project(project_id)

    async def close(self) -> None:
        projects = self._workers.projects() | self._recovery.recovered()
        await self._recovery.cancel()
        await self._workers.cancel()
        for p in projects:
            await self._recover_and_clear_project(p)
        self._registry.clear()
        self._workers.forget()
        self._recovery.forget()
