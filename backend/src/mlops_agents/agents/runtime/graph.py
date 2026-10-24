from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping
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


def format_invalid_tool_call_error(name: str, raw_args: str | None) -> str:
    """Format an error tool result message when tool call arguments are invalid JSON."""
    raw = "" if raw_args is None else str(raw_args)
    snippet = raw[:500]
    return f"The arguments of {name} were not valid JSON: {snippet}"


def _extract_call_index(call: Mapping[str, Any], fallback: int) -> int:
    idx = call.get("index")
    if idx is not None:
        try:
            return int(idx)
        except (ValueError, TypeError):
            pass
    call_id = str(call.get("id") or "")
    m = re.search(r"[_\-](\d+)$", call_id)
    if m:
        try:
            return int(m.group(1))
        except (ValueError, TypeError):
            pass
    return fallback


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
    lock: AbstractAsyncContextManager[Any],
    checkpointer: AsyncSqliteSaver | None = None,
) -> CompiledStateGraph[TurnState, None, TurnState, TurnState]:
    """Build and compile the StateGraph for a single turn."""

    async def _update_parts() -> None:
        async with lock:
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
            async with lock:
                await sink.text(message_id, stop_text)
                await store.update_message(project_id, message_id, parts=current_parts)
            return {
                "stopped_reason": "max_steps",
            }

        tool_schemas = [t.schema() for t in agent.tools]

        streamed_chunks: list[str] = []

        async def on_text(delta: str) -> None:
            streamed_chunks.append(delta)
            async with lock:
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

        valid_calls = list(reply.message.tool_calls or [])
        invalid_calls = list(reply.message.invalid_tool_calls or [])
        if valid_calls or invalid_calls:
            items: list[tuple[int, bool, dict[str, Any], str | None]] = []
            pos = 0
            for tc in valid_calls:
                order_key = _extract_call_index(tc, pos)
                items.append((order_key, False, dict(tc), None))
                pos += 1
            for itc in invalid_calls:
                order_key = _extract_call_index(itc, pos)
                raw_args = itc.get("args")
                items.append(
                    (
                        order_key,
                        True,
                        {"id": itc.get("id"), "name": itc.get("name"), "args": {}},
                        str(raw_args) if raw_args is not None else None,
                    )
                )
                pos += 1

            items.sort(key=lambda x: x[0])

            for _, is_invalid, tc_dict, _ in items:
                tc_part = ToolCallPart(
                    type="tool_call",
                    id=str(tc_dict["id"]),
                    name=str(tc_dict["name"]),
                    input={} if is_invalid else dict(tc_dict["args"]),
                )
                current_parts.append(tc_part)
                async with lock:
                    await sink.tool_started(message_id, tc_part)

            invalid_map = {str(item[2]["id"]): item[3] for item in items if item[1]}
            additional_kwargs = dict(reply.message.additional_kwargs)
            if invalid_map:
                additional_kwargs["invalid_tool_calls"] = invalid_map

            new_message = AIMessage(
                content=reply.message.content,
                tool_calls=[item[2] for item in items],
                invalid_tool_calls=[],
                response_metadata=reply.message.response_metadata,
                id=reply.message.id,
                additional_kwargs=additional_kwargs,
            )
        else:
            new_message = reply.message

        await _update_parts()

        return {
            "messages": [new_message],
            "step_count": step_count + 1,
        }

    async def tools_node(state: TurnState) -> dict[str, Any]:
        last_msg = state["messages"][-1]
        if not isinstance(last_msg, AIMessage):
            return {}

        all_calls = list(last_msg.tool_calls)
        invalid_calls_map: dict[str, str | None] = dict(
            last_msg.additional_kwargs.get("invalid_tool_calls") or {}
        )
        if last_msg.invalid_tool_calls:
            existing_ids = {str(tc.get("id")) for tc in all_calls}
            for itc in last_msg.invalid_tool_calls:
                itc_id = str(itc.get("id") or "")
                if itc_id and itc_id not in existing_ids:
                    all_calls.append({"id": itc_id, "name": itc.get("name") or "", "args": {}})
                if itc_id and itc_id not in invalid_calls_map:
                    raw = itc.get("args")
                    invalid_calls_map[itc_id] = str(raw) if raw is not None else None

        if not all_calls:
            return {}

        tool_messages: list[BaseMessage] = []
        for tc in all_calls:
            call_id = str(tc["id"])
            name = str(tc["name"])
            args = dict(tc["args"])

            if call_id in invalid_calls_map:
                raw_args = invalid_calls_map[call_id]
                error_msg = format_invalid_tool_call_error(name, raw_args)
                result_part = ToolResultPart(
                    type="tool_result",
                    tool_call_id=call_id,
                    content=error_msg,
                    is_error=True,
                )
            else:
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

            async with lock:
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
        if isinstance(last_msg, AIMessage) and (last_msg.tool_calls or last_msg.invalid_tool_calls):
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
