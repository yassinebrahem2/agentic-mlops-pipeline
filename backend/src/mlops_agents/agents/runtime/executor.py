from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Literal

from mlops_agents.agents.runtime.agent import Agent, AgentBuilder, TurnRefused, resume_command
from mlops_agents.agents.runtime.contract import (
    AuthorKind,
    ErrorKind,
    MessageStatus,
    MessageView,
    Part,
    TextPart,
    TurnEnd,
)
from mlops_agents.agents.runtime.graph import TurnHooks, TurnSink
from mlops_agents.agents.runtime.graph_run import run_turn_graph
from mlops_agents.agents.runtime.hooks import TurnObserver
from mlops_agents.agents.runtime.model_caller import ModelCaller, TurnFailed
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.runtime.waiting import UNEXPECTED_FAILURE_TEXT, delete_checkpoint_thread

logger = logging.getLogger(__name__)

__all__ = [
    "UNEXPECTED_FAILURE_TEXT",
    "TurnExecutor",
    "TurnOutcome",
    "TurnSink",
]

# How a turn ended: its end, error text and error kind; or that it waits for approval.
TurnOutcome = tuple[TurnEnd, str | None, ErrorKind | None] | tuple[Literal["waiting"], None, None]


@dataclass
class _AgentMessage:
    """A turn's agent message as its run builds it: its id once stored, and its live parts."""

    id: str | None = None
    parts: list[Part] = field(default_factory=lambda: [])


class TurnExecutor:
    def __init__(
        self,
        *,
        store: SessionStore,
        caller: ModelCaller,
        builder: AgentBuilder,
        hooks: TurnHooks,
        observers: Sequence[TurnObserver] = (),
    ) -> None:
        self._store = store
        self._caller = caller
        self._builder = builder
        self._hooks = hooks
        self._observers = tuple(observers)

    async def record_failure(
        self,
        project_id: str,
        session_id: str,
        turn_id: str,
        sink: TurnSink,
        lock: AbstractAsyncContextManager[Any],
        text: str,
        *,
        parts: list[Part] | None = None,
        error_kind: ErrorKind | None = None,
    ) -> None:
        """Leave a failed turn's visible end state: its message complete, plus a system message.

        The one owner of that state; the worker calls it too when an error escapes `run`.
        `parts=None` keeps the parts already stored for the message.

        A turn has one failure notice: a call made after an earlier one stored it (whose
        publish failed, say) writes no second one. A client reloads the turn's messages when
        the turn ends, so it still shows the stored one.

        `error_kind` is stored on the turn first, so the agent message published here carries
        it; the turn's end stores it again with the status.
        """
        async with lock:
            if error_kind is not None:
                await self._store.set_error_kind(project_id, turn_id, error_kind)
            notice_stored = False
            for message in await self._store.messages(project_id, session_id):
                if message.turn_id != turn_id:
                    continue
                if message.author_kind == "system":
                    notice_stored = True
                elif message.author_kind == "agent" and message.status == "streaming":
                    updated = await self._store.update_message(
                        project_id, message.id, parts=parts, status="complete"
                    )
                    await sink.message(updated, created=False)
            if notice_stored:
                return
            system_message = await self._store.add_message(
                project_id,
                session_id,
                turn_id=turn_id,
                author_kind="system",
                author_name="system",
                parts=[TextPart(type="text", text=text)],
                status="complete",
            )
            await sink.message(system_message, created=True)

    async def run(
        self,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str,
        sink: TurnSink,
        lock: AbstractAsyncContextManager[Any],
    ) -> TurnOutcome:
        message = _AgentMessage()

        async def add_message(
            author_kind: AuthorKind,
            author_name: str,
            parts: list[Part],
            status: MessageStatus,
        ) -> MessageView:
            # The stored write and its publish are one step under the session lock, so a
            # concurrent get() never returns a message whose event the subscriber gets again.
            async with lock:
                stored = await self._store.add_message(
                    project_id,
                    session_id,
                    turn_id=turn_id,
                    author_kind=author_kind,
                    author_name=author_name,
                    parts=parts,
                    status=status,
                )
                await sink.message(stored, created=True)
            return stored

        async def body() -> TurnOutcome:
            try:
                built_agent = await self._builder.build(
                    project_id, agent, session_id=session_id, turn_id=turn_id
                )
            except TurnRefused as exc:
                text_part: list[Part] = [TextPart(type="text", text=exc.text)]
                if exc.end == "done":
                    await add_message("agent", agent, text_part, "complete")
                    return ("done", None, None)
                await add_message("system", "system", text_part, "complete")
                return ("failed", exc.text, None)

            agent_msg = await add_message("agent", built_agent.definition.name, [], "streaming")
            message.id = agent_msg.id

            # The graph's `prepare` node compacts the session and builds the messages.
            initial_state: dict[str, Any] = {"step_count": 0, "stopped_reason": None}
            return await self._execute_graph(
                built_agent,
                project_id,
                session_id,
                turn_id,
                agent_msg.id,
                message.parts,
                initial_state,
                sink,
                lock,
            )

        return await self._guarded(project_id, session_id, turn_id, sink, lock, message, body)

    async def resume(
        self,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str,
        sink: TurnSink,
        lock: AbstractAsyncContextManager[Any],
    ) -> TurnOutcome:
        message = _AgentMessage()

        async def body() -> TurnOutcome:
            agent_msg = await self._store.turn_agent_message(project_id, session_id, turn_id)
            if agent_msg is None:
                raise RuntimeError(f"No agent message found for turn {turn_id}")
            message.id = agent_msg.id
            message.parts.extend(agent_msg.parts)

            async with lock:
                updated_msg = await self._store.update_message(
                    project_id, agent_msg.id, parts=message.parts, status="streaming"
                )
                await sink.message(updated_msg, created=False)
            await self._store.set_turn(project_id, turn_id, "running")

            built_agent = await self._builder.build(
                project_id, agent, session_id=session_id, turn_id=turn_id
            )
            summary, _ = await self._store.summary(project_id, session_id)
            return await self._execute_graph(
                built_agent,
                project_id,
                session_id,
                turn_id,
                agent_msg.id,
                message.parts,
                await resume_command(
                    built_agent, summary, self._store.db_path(project_id), turn_id
                ),
                sink,
                lock,
            )

        return await self._guarded(project_id, session_id, turn_id, sink, lock, message, body)

    async def delete_checkpoint(self, project_id: str, turn_id: str) -> None:
        """Delete a turn's checkpoint thread from the SQLite checkpointer."""
        await delete_checkpoint_thread(self._store.db_path(project_id), turn_id)

    async def _guarded(
        self,
        project_id: str,
        session_id: str,
        turn_id: str,
        sink: TurnSink,
        lock: AbstractAsyncContextManager[Any],
        message: _AgentMessage,
        body: Callable[[], Awaitable[TurnOutcome]],
    ) -> TurnOutcome:
        """Run a turn's body; a failure of it leaves the turn's failed end state.

        A cancel (Stop, close) marks the agent message stopped wherever it lands, also inside
        the failure handlers: a cancel raised from one of them is not caught by its siblings.
        A turn so stopped did not fail, so it takes back an error kind its failure record stored,
        before the stopped message is published.
        """
        recorded_kind: ErrorKind | None = None
        try:
            try:
                return await body()
            except TurnFailed as err:
                recorded_kind = err.error_kind
                await self.record_failure(
                    project_id,
                    session_id,
                    turn_id,
                    sink,
                    lock,
                    err.message,
                    parts=message.parts,
                    error_kind=err.error_kind,
                )
                return ("failed", err.message, err.error_kind)
            except Exception:
                logger.exception("Turn %s failed unexpectedly", turn_id)
                await self.record_failure(
                    project_id,
                    session_id,
                    turn_id,
                    sink,
                    lock,
                    UNEXPECTED_FAILURE_TEXT,
                    parts=message.parts,
                )
                return ("failed", UNEXPECTED_FAILURE_TEXT, None)
        except asyncio.CancelledError:
            if recorded_kind is not None:
                await self._store.set_error_kind(project_id, turn_id, None)
            if message.id is not None:
                await self._finish_message(
                    project_id, sink, lock, message.id, message.parts, "stopped"
                )
            raise

    async def _finish_message(
        self,
        project_id: str,
        sink: TurnSink,
        lock: AbstractAsyncContextManager[Any],
        message_id: str,
        parts: list[Part],
        status: MessageStatus,
    ) -> None:
        """Store the agent message's parts with its final status."""
        async with lock:
            updated = await self._store.update_message(
                project_id, message_id, parts=parts, status=status
            )
            await sink.message(updated, created=False)

    async def _execute_graph(
        self,
        built_agent: Agent,
        project_id: str,
        session_id: str,
        turn_id: str,
        message_id: str,
        parts: list[Part],
        graph_input: Any,
        sink: TurnSink,
        lock: AbstractAsyncContextManager[Any],
    ) -> TurnOutcome:
        """Run the turn graph from `graph_input` (a new turn's state, or a resume) to its end,
        or to a wait for approval."""
        waits = await run_turn_graph(
            agent=built_agent,
            caller=self._caller,
            store=self._store,
            hooks=self._hooks,
            observers=self._observers,
            project_id=project_id,
            session_id=session_id,
            turn_id=turn_id,
            message_id=message_id,
            parts=parts,
            graph_input=graph_input,
            sink=sink,
            lock=lock,
        )
        if waits:
            return ("waiting", None, None)
        await self._finish_message(project_id, sink, lock, message_id, parts, "complete")
        return ("done", None, None)
