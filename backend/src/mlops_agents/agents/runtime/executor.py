from __future__ import annotations

import asyncio
import logging
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiosqlite

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

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
from mlops_agents.agents.runtime.graph import TurnSink, TurnState, create_turn_graph
from mlops_agents.agents.runtime.model_caller import ModelCaller, TurnFailed
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.storage.sqlite import connect_checkpointer

logger = logging.getLogger(__name__)

__all__ = ["UNEXPECTED_FAILURE_TEXT", "TurnExecutor", "TurnSink"]

UNEXPECTED_FAILURE_TEXT = "The turn failed unexpectedly. The details are in the server log."


class TurnExecutor:
    def __init__(self, *, store: SessionStore, caller: ModelCaller, builder: AgentBuilder) -> None:
        self._store = store
        self._caller = caller
        self._builder = builder

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
        conn: aiosqlite.Connection | None = None
        checkpointer: AsyncSqliteSaver | None = None

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
            await finish_agent_message("complete")
            await add_message("system", "system", [TextPart(type="text", text=text)], "complete")
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

            conn = await connect_checkpointer(self._store.db_path(project_id))
            checkpointer = AsyncSqliteSaver(conn)
            await checkpointer.setup()

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

            graph = create_turn_graph(
                agent=built_agent,
                caller=self._caller,
                store=self._store,
                sink=sink,
                project_id=project_id,
                turn_id=turn_id,
                message_id=agent_message_id,
                current_parts=current_parts,
                checkpointer=checkpointer,
                lock=lock,
            )

            config: RunnableConfig = {"configurable": {"thread_id": turn_id}}
            initial_state: TurnState = {
                "messages": initial_messages,
                "step_count": 0,
                "stopped_reason": None,
            }
            await graph.ainvoke(initial_state, config=config)  # pyright: ignore[reportUnknownMemberType]  # LangGraph ainvoke return type is partially untyped

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

        finally:
            try:
                if checkpointer is not None:
                    await checkpointer.adelete_thread(turn_id)
            except Exception:
                logger.exception("Could not delete the checkpointer thread of turn %s", turn_id)
            finally:
                # Windows keeps the project folder locked until the connection is closed, also
                # when a second cancel interrupts the thread cleanup.
                if conn is not None:
                    await conn.close()
