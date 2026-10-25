from __future__ import annotations

import asyncio
import time
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Annotated, Any, NotRequired, Protocol, TypedDict

import aiosqlite
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph.message import add_messages  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph.state import CompiledStateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.types import Command

from mlops_agents.agents.runtime.agent import Agent
from mlops_agents.agents.runtime.contract import (
    MessageView,
    Part,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from mlops_agents.agents.runtime.hooks import Gate, GateDecision, InboundSource, TurnEndCheck
from mlops_agents.agents.runtime.model_caller import ModelCaller
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.runtime.tool_step import ToolStep, needs_wait
from mlops_agents.agents.tasks.contract import TaskWorkspaces
from mlops_agents.agents.tools.base import Preview


class TurnSink(Protocol):
    async def text(self, message_id: str, text: str) -> None: ...
    async def tool_started(self, message_id: str, call: ToolCallPart) -> None: ...
    async def tool_finished(self, message_id: str, result: ToolResultPart) -> None: ...
    async def message(self, message: MessageView, *, created: bool) -> None: ...
    async def turn_waiting(self, turn_id: str) -> None: ...


class TurnState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    step_count: int
    stopped_reason: str | None
    decisions: NotRequired[dict[str, GateDecision]]  # the gate's, for the step's tool calls
    call_errors: NotRequired[dict[str, str]]  # the step's calls answered before the gate
    turn_end_checked: NotRequired[bool]
    end_note: NotRequired[str | None]  # for the next model request only, never a message


@dataclass(frozen=True)
class TurnHooks:
    gate: Gate
    inbound: InboundSource
    turn_end: TurnEndCheck
    workspaces: TaskWorkspaces


def turn_checkpointer(conn: aiosqlite.Connection) -> AsyncSqliteSaver:
    """A checkpointer that reads back LangChain's messages and the gate's types in the state.

    Only these and LangGraph's safe types are read back as themselves; any other type would come
    back a plain dict. A type added to `TurnState` beyond LangChain's messages is added here.
    """
    allowed = [(t.__module__, t.__name__) for t in (GateDecision, Preview, Command)]
    return AsyncSqliteSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=allowed))


def format_invalid_tool_call_error(name: str, raw_args: str | None) -> str:
    """Format an error tool result message when tool call arguments are invalid JSON."""
    raw = "" if raw_args is None else str(raw_args)
    snippet = raw[:500]
    return f"The arguments of {name} were not valid JSON: {snippet}"


def create_turn_graph(
    *,
    agent: Agent,
    caller: ModelCaller,
    store: SessionStore,
    sink: TurnSink,
    project_id: str,
    session_id: str,
    turn_id: str,
    message_id: str,
    current_parts: list[Part],
    lock: AbstractAsyncContextManager[Any],
    hooks: TurnHooks,
    checkpointer: AsyncSqliteSaver | None = None,
) -> CompiledStateGraph[TurnState, None, TurnState, TurnState]:
    """Build and compile the StateGraph for a single turn."""

    async def _update_parts() -> None:
        async with lock:
            await store.update_message(project_id, message_id, parts=current_parts)

    async def model_node(state: TurnState) -> dict[str, Any]:
        step_count = state.get("step_count", 0)
        end_note = state.get("end_note")
        # The step a turn-end note causes (at most one per turn) neither checks nor spends the
        # step budget, so a turn that ends exactly at the limit still gets it.
        if not end_note and agent.max_steps is not None and step_count >= agent.max_steps:
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

        inbound = await hooks.inbound.take(project_id, session_id, turn_id)
        request = [*state["messages"], *inbound]
        if end_note:
            request.append(HumanMessage(content=end_note))
        tool_schemas = [t.schema() for t in agent.tools]

        streamed_chunks: list[str] = []

        async def on_text(delta: str) -> None:
            streamed_chunks.append(delta)
            async with lock:
                await sink.text(message_id, delta)

        t0 = time.monotonic()
        try:
            reply = await caller.call(agent.model, request, tool_schemas, on_text)
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
            items: list[tuple[int, dict[str, Any]]] = []
            tool_errors: dict[str, str] = {}
            pos = 0
            for tc in valid_calls:
                call_id = str(tc["id"])
                order_key = reply.tool_call_indices.get(call_id, pos)
                items.append((order_key, dict(tc)))
                pos += 1
            for itc in invalid_calls:
                call_id = str(itc.get("id") or "")
                order_key = reply.tool_call_indices.get(call_id, pos)
                raw_args = itc.get("args")
                name = str(itc.get("name") or "")
                items.append(
                    (
                        order_key,
                        {
                            "id": call_id,
                            "name": name,
                            "args": {},
                        },
                    )
                )
                tool_errors[call_id] = format_invalid_tool_call_error(
                    name,
                    str(raw_args) if raw_args is not None else None,
                )
                pos += 1

            items.sort(key=lambda x: x[0])
            converted_calls = [item[1] for item in items]

            for tc_dict in converted_calls:
                tc_part = ToolCallPart(
                    type="tool_call",
                    id=str(tc_dict["id"]),
                    name=str(tc_dict["name"]),
                    input=dict(tc_dict["args"]),
                )
                current_parts.append(tc_part)
                async with lock:
                    await sink.tool_started(message_id, tc_part)

            additional_kwargs = dict(reply.message.additional_kwargs)
            if tool_errors:
                additional_kwargs["tool_errors"] = tool_errors

            new_message = AIMessage(
                content=reply.message.content,
                tool_calls=converted_calls,
                invalid_tool_calls=[],
                response_metadata=reply.message.response_metadata,
                id=reply.message.id,
                additional_kwargs=additional_kwargs,
            )
        else:
            new_message = reply.message

        await _update_parts()

        return {
            "messages": [*inbound, new_message],
            "step_count": step_count if end_note else step_count + 1,
            "end_note": None,
        }

    step = ToolStep(
        agent=agent,
        gate=hooks.gate,
        workspaces=hooks.workspaces,
        sink=sink,
        project_id=project_id,
        session_id=session_id,
        turn_id=turn_id,
        message_id=message_id,
        current_parts=current_parts,
        lock=lock,
        update_parts=_update_parts,
    )

    async def turn_end_node(state: TurnState) -> dict[str, Any]:
        note = await hooks.turn_end.note(project_id, session_id, turn_id)
        return {"turn_end_checked": True, "end_note": note}

    def route_model(state: TurnState) -> str:
        if state.get("stopped_reason"):
            return END
        last_msg = state["messages"][-1]
        if isinstance(last_msg, AIMessage) and bool(last_msg.tool_calls):
            return "decide"
        if not state.get("turn_end_checked"):
            return "turn_end"
        return END

    def route_decide(state: TurnState) -> str:
        return "wait" if needs_wait(state) else "tools"

    def route_turn_end(state: TurnState) -> str:
        return "model" if state.get("end_note") else END

    graph_builder = StateGraph(TurnState)
    graph_builder.add_node("model", model_node)  # pyright: ignore[reportUnknownMemberType]  # LangGraph add_node overload is partially untyped
    graph_builder.add_node("decide", step.decide)  # pyright: ignore[reportUnknownMemberType]  # LangGraph add_node overload is partially untyped
    graph_builder.add_node("wait", step.wait)  # pyright: ignore[reportUnknownMemberType]  # LangGraph add_node overload is partially untyped
    graph_builder.add_node("tools", step.run)  # pyright: ignore[reportUnknownMemberType]  # LangGraph add_node overload is partially untyped
    graph_builder.add_node("turn_end", turn_end_node)  # pyright: ignore[reportUnknownMemberType]  # LangGraph add_node overload is partially untyped
    graph_builder.add_edge(START, "model")
    graph_builder.add_conditional_edges(
        "model", route_model, {"decide": "decide", "turn_end": "turn_end", END: END}
    )
    graph_builder.add_conditional_edges("decide", route_decide, {"wait": "wait", "tools": "tools"})
    graph_builder.add_edge("wait", "tools")
    graph_builder.add_edge("tools", "model")
    graph_builder.add_conditional_edges("turn_end", route_turn_end, {"model": "model", END: END})

    if checkpointer is not None:
        return graph_builder.compile(checkpointer=checkpointer)  # pyright: ignore[reportUnknownMemberType]  # LangGraph compile parameters are partially untyped
    return graph_builder.compile()  # pyright: ignore[reportUnknownMemberType]  # LangGraph compile return type is partially untyped
