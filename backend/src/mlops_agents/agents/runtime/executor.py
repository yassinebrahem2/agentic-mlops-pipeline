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
    Part,
    TextPart,
    TurnEnd,
)
from mlops_agents.agents.runtime.graph import TurnSink, TurnState, create_turn_graph
from mlops_agents.agents.runtime.model_caller import ModelCaller, TurnFailed
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.storage.sqlite import connect_checkpointer

logger = logging.getLogger(__name__)

__all__ = ["TurnExecutor", "TurnSink"]


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
        lock: AbstractAsyncContextManager[Any] | None = None,
    ) -> tuple[TurnEnd, str | None]:
        agent_message_id: str | None = None
        current_parts: list[Part] = []
        conn: aiosqlite.Connection | None = None
        checkpointer: AsyncSqliteSaver | None = None

        try:
            # 1. Build agent
            try:
                built_agent = await self._builder.build(project_id, agent)
            except TurnRefused as exc:
                if exc.end == "done":
                    if lock is not None:
                        async with lock:
                            agent_msg = await self._store.add_message(
                                project_id,
                                session_id,
                                turn_id=turn_id,
                                author_kind="agent",
                                author_name=agent,
                                parts=[TextPart(type="text", text=exc.text)],
                                status="complete",
                            )
                            await sink.message(agent_msg, created=True)
                    else:
                        agent_msg = await self._store.add_message(
                            project_id,
                            session_id,
                            turn_id=turn_id,
                            author_kind="agent",
                            author_name=agent,
                            parts=[TextPart(type="text", text=exc.text)],
                            status="complete",
                        )
                        await sink.message(agent_msg, created=True)
                    return ("done", None)
                else:
                    if lock is not None:
                        async with lock:
                            sys_msg = await self._store.add_message(
                                project_id,
                                session_id,
                                turn_id=turn_id,
                                author_kind="system",
                                author_name="system",
                                parts=[TextPart(type="text", text=exc.text)],
                                status="complete",
                            )
                            await sink.message(sys_msg, created=True)
                    else:
                        sys_msg = await self._store.add_message(
                            project_id,
                            session_id,
                            turn_id=turn_id,
                            author_kind="system",
                            author_name="system",
                            parts=[TextPart(type="text", text=exc.text)],
                            status="complete",
                        )
                        await sink.message(sys_msg, created=True)
                    return ("failed", exc.text)

            # 2. Create streaming agent message
            if lock is not None:
                async with lock:
                    agent_msg = await self._store.add_message(
                        project_id,
                        session_id,
                        turn_id=turn_id,
                        author_kind="agent",
                        author_name=built_agent.definition.name,
                        parts=[],
                        status="streaming",
                    )
                    await sink.message(agent_msg, created=True)
            else:
                agent_msg = await self._store.add_message(
                    project_id,
                    session_id,
                    turn_id=turn_id,
                    author_kind="agent",
                    author_name=built_agent.definition.name,
                    parts=[],
                    status="streaming",
                )
                await sink.message(agent_msg, created=True)
            agent_message_id = agent_msg.id

            db_path = self._store.db_path(project_id)
            conn = await connect_checkpointer(db_path)
            checkpointer = AsyncSqliteSaver(conn)
            await checkpointer.setup()

            # 3. Compaction
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

            # 4. Initial messages for graph
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

            # 5. Graph
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

            if lock is not None:
                async with lock:
                    final_agent_msg = await self._store.update_message(
                        project_id, agent_message_id, parts=current_parts, status="complete"
                    )
                    await sink.message(final_agent_msg, created=False)
            else:
                final_agent_msg = await self._store.update_message(
                    project_id, agent_message_id, parts=current_parts, status="complete"
                )
                await sink.message(final_agent_msg, created=False)

            return ("done", None)

        except asyncio.CancelledError:
            if agent_message_id is not None:
                if lock is not None:
                    async with lock:
                        final_msg = await self._store.update_message(
                            project_id, agent_message_id, parts=current_parts, status="stopped"
                        )
                        await sink.message(final_msg, created=False)
                else:
                    final_msg = await self._store.update_message(
                        project_id, agent_message_id, parts=current_parts, status="stopped"
                    )
                    await sink.message(final_msg, created=False)
            raise

        except TurnFailed as err:
            if agent_message_id is not None:
                if lock is not None:
                    async with lock:
                        final_agent_msg = await self._store.update_message(
                            project_id, agent_message_id, parts=current_parts, status="complete"
                        )
                        await sink.message(final_agent_msg, created=False)
                else:
                    final_agent_msg = await self._store.update_message(
                        project_id, agent_message_id, parts=current_parts, status="complete"
                    )
                    await sink.message(final_agent_msg, created=False)

            if lock is not None:
                async with lock:
                    sys_msg = await self._store.add_message(
                        project_id,
                        session_id,
                        turn_id=turn_id,
                        author_kind="system",
                        author_name="system",
                        parts=[TextPart(type="text", text=err.message)],
                        status="complete",
                    )
                    await sink.message(sys_msg, created=True)
            else:
                sys_msg = await self._store.add_message(
                    project_id,
                    session_id,
                    turn_id=turn_id,
                    author_kind="system",
                    author_name="system",
                    parts=[TextPart(type="text", text=err.message)],
                    status="complete",
                )
                await sink.message(sys_msg, created=True)

            return ("failed", err.message)

        except Exception as exc:
            logger.exception("Turn executor failed for turn %s: %s", turn_id, exc)
            err_text = str(exc) or type(exc).__name__
            if agent_message_id is not None:
                if lock is not None:
                    async with lock:
                        final_agent_msg = await self._store.update_message(
                            project_id, agent_message_id, parts=current_parts, status="complete"
                        )
                        await sink.message(final_agent_msg, created=False)
                else:
                    final_agent_msg = await self._store.update_message(
                        project_id, agent_message_id, parts=current_parts, status="complete"
                    )
                    await sink.message(final_agent_msg, created=False)

            if lock is not None:
                async with lock:
                    sys_msg = await self._store.add_message(
                        project_id,
                        session_id,
                        turn_id=turn_id,
                        author_kind="system",
                        author_name="system",
                        parts=[TextPart(type="text", text=err_text)],
                        status="complete",
                    )
                    await sink.message(sys_msg, created=True)
            else:
                sys_msg = await self._store.add_message(
                    project_id,
                    session_id,
                    turn_id=turn_id,
                    author_kind="system",
                    author_name="system",
                    parts=[TextPart(type="text", text=err_text)],
                    status="complete",
                )
                await sink.message(sys_msg, created=True)

            return ("failed", err_text)

        finally:
            if checkpointer is not None:
                try:
                    await checkpointer.adelete_thread(turn_id)
                except Exception as exc:
                    logger.exception("Failed to delete checkpointer thread %s: %s", turn_id, exc)
            if conn is not None:
                await conn.close()
