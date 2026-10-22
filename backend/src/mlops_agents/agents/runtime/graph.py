from __future__ import annotations

import asyncio
import time
from contextlib import AbstractAsyncContextManager
from typing import Annotated, Any, Protocol, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph.message import add_messages  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph.state import CompiledStateGraph  # pyright: ignore[reportMissingTypeStubs]

from mlops_agents.agents.runtime.agent import Agent
from mlops_agents.agents.runtime.contract import (
    MessageView,
    Part,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from mlops_agents.agents.runtime.model_caller import ModelCaller
from mlops_agents.agents.runtime.store import SessionStore


class TurnSink(Protocol):
    async def text(self, message_id: str, text: str) -> None: ...
    async def tool_started(self, message_id: str, call: ToolCallPart) -> None: ...
    async def tool_finished(self, message_id: str, result: ToolResultPart) -> None: ...
    async def message(self, message: MessageView, *, created: bool) -> None: ...


class TurnState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    step_count: int
    stopped_reason: str | None


def create_turn_graph(
    *,
    agent: Agent,
    caller: ModelCaller,
    store: SessionStore,
    sink: TurnSink,
    project_id: str,
    turn_id: str,
    message_id: str,
    current_parts: list[Part],
    checkpointer: AsyncSqliteSaver | None = None,
    lock: AbstractAsyncContextManager[Any] | None = None,
) -> CompiledStateGraph[TurnState, None, TurnState, TurnState]:
    """Build and compile the StateGraph for a single turn."""

    async def _update_parts() -> None:
        if lock is not None:
            async with lock:
                await store.update_message(project_id, message_id, parts=current_parts)
        else:
            await store.update_message(project_id, message_id, parts=current_parts)

    async def model_node(state: TurnState) -> dict[str, Any]:
        step_count = state.get("step_count", 0)

        # Before each model step: check if max_steps limit is reached
        if agent.max_steps is not None and step_count >= agent.max_steps:
            stop_text = (
                f"Stopped after {agent.max_steps} steps (this agent's step limit). "
                "Send a message to continue."
            )
            stop_part = TextPart(type="text", text=stop_text)
            current_parts.append(stop_part)
            if lock is not None:
                async with lock:
                    await sink.text(message_id, stop_text)
                    await store.update_message(project_id, message_id, parts=current_parts)
            else:
                await sink.text(message_id, stop_text)
                await store.update_message(project_id, message_id, parts=current_parts)
            return {
                "stopped_reason": "max_steps",
            }

        tool_schemas = [t.schema() for t in agent.tools]

        streamed_chunks: list[str] = []

        async def on_text(delta: str) -> None:
            streamed_chunks.append(delta)
            if lock is not None:
                async with lock:
                    await sink.text(message_id, delta)
            else:
                await sink.text(message_id, delta)

        t0 = time.monotonic()
        try:
            reply = await caller.call(agent.model, state["messages"], tool_schemas, on_text)
        except (Exception, asyncio.CancelledError):
            if streamed_chunks:
                partial_text = "".join(streamed_chunks)
                current_parts.append(TextPart(type="text", text=partial_text))
                await _update_parts()
            raise
        latency_ms = int((time.monotonic() - t0) * 1000)

        await store.add_llm_call(
            project_id,
            turn_id,
            agent=agent.definition.name,
            model_address=agent.model.address,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            latency_ms=latency_ms,
        )

        if reply.message.content:
            content_str = (
                reply.message.content
                if isinstance(reply.message.content, str)
                else "".join(str(c) for c in reply.message.content)
            )
            if content_str:
                current_parts.append(TextPart(type="text", text=content_str))

        if reply.message.tool_calls:
            for tc in reply.message.tool_calls:
                tc_part = ToolCallPart(
                    type="tool_call",
                    id=str(tc["id"]),
                    name=str(tc["name"]),
                    input=dict(tc["args"]),
                )
                current_parts.append(tc_part)
                if lock is not None:
                    async with lock:
                        await sink.tool_started(message_id, tc_part)
                else:
                    await sink.tool_started(message_id, tc_part)

        await _update_parts()

        return {
            "messages": [reply.message],
            "step_count": step_count + 1,
        }

    async def tools_node(state: TurnState) -> dict[str, Any]:
        last_msg = state["messages"][-1]
        if not isinstance(last_msg, AIMessage) or not last_msg.tool_calls:
            return {}

        tool_messages: list[BaseMessage] = []
        for tc in last_msg.tool_calls:
            call_id = str(tc["id"])
            name = str(tc["name"])
            args = dict(tc["args"])

            # Look up tool only in agent.tools, never in the registry
            tool = next((t for t in agent.tools if t.name == name), None)
            if tool is not None:
                output = await tool.invoke(agent.tool_context, args)
                result_part = ToolResultPart(
                    type="tool_result",
                    tool_call_id=call_id,
                    content=output.content,
                    is_error=output.is_error,
                )
            else:
                result_part = ToolResultPart(
                    type="tool_result",
                    tool_call_id=call_id,
                    content=f"There is no tool named {name}.",
                    is_error=True,
                )

            if lock is not None:
                async with lock:
                    await sink.tool_finished(message_id, result_part)
            else:
                await sink.tool_finished(message_id, result_part)
            current_parts.append(result_part)
            tool_messages.append(ToolMessage(tool_call_id=call_id, content=result_part.content))

        await _update_parts()

        return {
            "messages": tool_messages,
        }

    def route_model(state: TurnState) -> str:
        if state.get("stopped_reason"):
            return END
        last_msg = state["messages"][-1]
        if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
            return "tools"
        return END

    graph_builder = StateGraph(TurnState)
    graph_builder.add_node("model", model_node)  # pyright: ignore[reportUnknownMemberType]  # LangGraph add_node overload is partially untyped
    graph_builder.add_node("tools", tools_node)  # pyright: ignore[reportUnknownMemberType]  # LangGraph add_node overload is partially untyped
    graph_builder.add_edge(START, "model")
    graph_builder.add_conditional_edges("model", route_model, {"tools": "tools", END: END})
    graph_builder.add_edge("tools", "model")

    if checkpointer is not None:
        return graph_builder.compile(checkpointer=checkpointer)  # pyright: ignore[reportUnknownMemberType]  # LangGraph compile parameters are partially untyped
    return graph_builder.compile()  # pyright: ignore[reportUnknownMemberType]  # LangGraph compile return type is partially untyped
