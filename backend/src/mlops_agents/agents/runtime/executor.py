from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphInterrupt
from langgraph.types import Durability

from mlops_agents.agents.runtime.agent import AgentBuilder, TurnRefused
from mlops_agents.agents.runtime.compaction import compact_if_needed
from mlops_agents.agents.runtime.context import build_messages
from mlops_agents.agents.runtime.contract import (
    AuthorKind,
    MessageStatus,
    MessageView,
    Part,
    TextPart,
    TurnEnd,
)
from mlops_agents.agents.runtime.graph import (
    TurnHooks,
    TurnSink,
    TurnState,
    create_turn_graph,
    turn_checkpointer,
)
from mlops_agents.agents.runtime.hooks import GateDecision, TurnObserver
from mlops_agents.agents.runtime.model_caller import ModelCaller, TurnFailed
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.runtime.waiting import handle_turn_waiting
from mlops_agents.storage.sqlite import connect_checkpointer

logger = logging.getLogger(__name__)

__all__ = ["TURN_DURABILITY", "UNEXPECTED_FAILURE_TEXT", "TurnExecutor", "TurnSink"]

UNEXPECTED_FAILURE_TEXT = "The turn failed unexpectedly. The details are in the server log."

# "async" runs each checkpoint write as a background task that a cancel does not cancel, and the
# run's exit waits for it: a Stop or close_project never leaves a checkpoint transaction open,
# which would lock the project file. "sync" awaits the write in the turn's own task, where a
# cancel lands inside it.
TURN_DURABILITY: Durability = "async"


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
    ) -> None:
        """Leave a failed turn's visible end state: its message complete, plus a system message.

        The one owner of that state; the worker calls it too when an error escapes `run`.
        `parts=None` keeps the parts already stored for the message.
        """
        async with lock:
            for message in await self._store.messages(project_id, session_id):
                if (
                    message.turn_id == turn_id
                    and message.author_kind == "agent"
                    and message.status == "streaming"
                ):
                    updated = await self._store.update_message(
                        project_id, message.id, parts=parts, status="complete"
                    )
                    await sink.message(updated, created=False)
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
    ) -> tuple[TurnEnd, str | None]:
        agent_message_id: str | None = None
        current_parts: list[Part] = []

        async def add_message(
            author_kind: AuthorKind,
            author_name: str,
            parts: list[Part],
            status: MessageStatus,
        ) -> MessageView:
            # The stored write and its publish are one step under the session lock, so a
            # concurrent get() never returns a message whose event the subscriber gets again.
            async with lock:
                message = await self._store.add_message(
                    project_id,
                    session_id,
                    turn_id=turn_id,
                    author_kind=author_kind,
                    author_name=author_name,
                    parts=parts,
                    status=status,
                )
                await sink.message(message, created=True)
            return message

        async def finish_agent_message(status: MessageStatus) -> None:
            if agent_message_id is None:
                return
            async with lock:
                message = await self._store.update_message(
                    project_id, agent_message_id, parts=current_parts, status=status
                )
                await sink.message(message, created=False)

        async def fail(text: str) -> tuple[TurnEnd, str | None]:
            await self.record_failure(
                project_id, session_id, turn_id, sink, lock, text, parts=current_parts
            )
            return ("failed", text)

        try:
            try:
                built_agent = await self._builder.build(project_id, agent)
            except TurnRefused as exc:
                text_part: list[Part] = [TextPart(type="text", text=exc.text)]
                if exc.end == "done":
                    await add_message("agent", agent, text_part, "complete")
                    return ("done", None)
                await add_message("system", "system", text_part, "complete")
                return ("failed", exc.text)

            agent_msg = await add_message("agent", built_agent.definition.name, [], "streaming")
            agent_message_id = agent_msg.id

            summary, compacted_through = await self._store.summary(project_id, session_id)
            history = await self._store.messages(project_id, session_id)
            history_for_compaction = [m for m in history if m.id != agent_message_id]
            system_msg = build_messages(
                built_agent.definition, built_agent.project, built_agent.layout, summary, []
            )[0]
            await compact_if_needed(
                self._store,
                self._caller,
                built_agent.model,
                project_id,
                session_id,
                history_for_compaction,
                str(system_msg.content),
                turn_id=turn_id,
                agent_name=built_agent.definition.name,
            )

            summary, compacted_through = await self._store.summary(project_id, session_id)
            active_history = await self._store.messages(
                project_id, session_id, after=compacted_through
            )
            active_history = [m for m in active_history if m.id != agent_message_id]
            initial_messages = build_messages(
                built_agent.definition,
                built_agent.project,
                built_agent.layout,
                summary,
                active_history,
            )

            is_interrupted = False
            decisions_to_record: dict[str, GateDecision] | None = None

            # The checkpointer connection is closed before any handler below writes the end state,
            # so once a turn reports how it ended, nothing of it still holds the project file.
            async with self._checkpointer(project_id, turn_id) as cp:
                graph = create_turn_graph(
                    agent=built_agent,
                    caller=self._caller,
                    store=self._store,
                    sink=sink,
                    project_id=project_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    message_id=agent_message_id,
                    current_parts=current_parts,
                    checkpointer=cp.saver,
                    lock=lock,
                    hooks=self._hooks,
                )
                config: RunnableConfig = {"configurable": {"thread_id": turn_id}}
                initial_state: TurnState = {
                    "messages": initial_messages,
                    "step_count": 0,
                    "stopped_reason": None,
                }
                try:
                    res = await graph.ainvoke(  # pyright: ignore[reportUnknownMemberType]
                        initial_state, config=config, durability=TURN_DURABILITY
                    )
                    if isinstance(res, dict) and bool(res.get("__interrupt__")):  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
                        is_interrupted = True
                    else:
                        chk_state = await graph.aget_state(config)
                        if chk_state.tasks and any(t.interrupts for t in chk_state.tasks):
                            is_interrupted = True
                except GraphInterrupt:
                    is_interrupted = True

                if is_interrupted:
                    cp.preserved = True
                    try:
                        chk_state = await graph.aget_state(config)
                        decisions_to_record = chk_state.values.get("decisions")
                    except Exception:
                        logger.debug(
                            "Could not read graph state decisions for turn %s",
                            turn_id,
                            exc_info=True,
                        )

            if is_interrupted:
                await handle_turn_waiting(
                    store=self._store,
                    sink=sink,
                    project_id=project_id,
                    session_id=session_id,
                    agent=built_agent.definition.name,
                    turn_id=turn_id,
                    message_id=agent_message_id,
                    current_parts=current_parts,
                    decisions=decisions_to_record,
                    lock=lock,
                    observers=self._observers,
                )
                return cast(tuple[TurnEnd, str | None], ("waiting", None))

            await finish_agent_message("complete")
            return ("done", None)

        except asyncio.CancelledError:
            await finish_agent_message("stopped")
            raise

        except TurnFailed as err:
            return await fail(err.message)

        except Exception:
            logger.exception("Turn %s failed unexpectedly", turn_id)
            return await fail(UNEXPECTED_FAILURE_TEXT)

    @asynccontextmanager
    async def _checkpointer(
        self, project_id: str, turn_id: str
    ) -> AsyncGenerator[_CheckpointHolder]:
        conn = await connect_checkpointer(self._store.db_path(project_id))
        try:
            saver = turn_checkpointer(conn)
            await saver.setup()
            holder = _CheckpointHolder(saver)
            try:
                yield holder
            finally:
                if not holder.preserved:
                    try:
                        await saver.adelete_thread(turn_id)
                    except Exception:
                        logger.exception(
                            "Could not delete the checkpointer thread of turn %s", turn_id
                        )
        finally:
            # Windows keeps the project folder locked until the connection is closed, also
            # when a second cancel interrupts the thread cleanup.
            await conn.close()


class _CheckpointHolder:
    def __init__(self, saver: AsyncSqliteSaver) -> None:
        self.saver = saver
        self.preserved = False
