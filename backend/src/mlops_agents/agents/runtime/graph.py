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

from mlops_agents.agents.approvals.authority import Authority
from mlops_agents.agents.approvals.contract import Approvals
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.agent import Agent
from mlops_agents.agents.runtime.budget import (
    REQUEST_TOO_LARGE_MESSAGE,
    ROOM_FLOOR_TOKENS,
    fixed_tokens,
    request_budget_tokens,
    room_tokens,
)
from mlops_agents.agents.runtime.compaction import (
    compact_if_needed,
    group_turns,
    message_len,
)
from mlops_agents.agents.runtime.context import history_messages, system_message
from mlops_agents.agents.runtime.contract import (
    MessageView,
    Part,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from mlops_agents.agents.runtime.hooks import Gate, GateDecision, InboundSource, TurnEndCheck
from mlops_agents.agents.runtime.model_caller import (
    ModelCaller,
    ModelKeysExhausted,
    TurnFailed,
)
from mlops_agents.agents.runtime.stall import StallState, await_stall, pending_stall, record_stall
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.runtime.tool_step import ToolStep, needs_wait
from mlops_agents.agents.tasks.contract import TaskWorkspaces
from mlops_agents.agents.tools.base import Preview
from mlops_agents.events.contract import EventLog
from mlops_agents.providers.errors import ProviderCallError, ProviderErrorKind


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
    stall: NotRequired[StallState | None]  # the step's stall: no usable key (stall.py)


@dataclass(frozen=True)
class TurnHooks:
    gate: Gate
    inbound: InboundSource
    turn_end: TurnEndCheck
    workspaces: TaskWorkspaces
    approval_store: ApprovalStore  # the app's one store
    approvals: Approvals  # a stall's approval
    events: EventLog | None  # turn.stalled and turn.resumed; None records no event


def turn_checkpointer(conn: aiosqlite.Connection) -> AsyncSqliteSaver:
    """A checkpointer that reads back LangChain's messages and the gate's types in the state.

    Only these and LangGraph's safe types are read back as themselves; any other type would come
    back a plain dict. A type added to `TurnState` beyond LangChain's messages is added here.
    """
    allowed = [
        (t.__module__, t.__name__) for t in (GateDecision, Preview, Command, StallState, Authority)
    ]
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

    async def prepare_node(state: TurnState) -> dict[str, Any]:
        """The turn's first node: compact the session when it needs it, then build the turn's
        messages. Compaction's model call stalls the turn as the model node's does; compaction
        reads the store again, so a run after Try again starts over and a stalled one wrote
        nothing."""
        summary, _ = await store.summary(project_id, session_id)
        history = await store.messages(project_id, session_id)
        tool_schemas = [t.schema() for t in agent.tools]
        try:
            await compact_if_needed(
                store,
                caller,
                agent.model,
                project_id,
                session_id,
                [m for m in history if m.id != message_id],
                str(system_message(agent, summary).content),
                turn_id=turn_id,
                agent_name=agent.definition.name,
                tools=tool_schemas,
            )
        except ModelKeysExhausted as error:
            stall = await record_stall(
                hooks.approvals,
                hooks.events,
                project_id=project_id,
                agent=agent.definition.name,
                turn_id=turn_id,
                step=0,
                exhausted=error,
                node="prepare",
            )
            return {"stall": stall}
        except ProviderCallError as comp_err:
            if comp_err.kind is ProviderErrorKind.REQUEST_TOO_LARGE:
                raise TurnFailed(REQUEST_TOO_LARGE_MESSAGE) from comp_err
            raise
        summary, compacted_through = await store.summary(project_id, session_id)
        active = await store.messages(project_id, session_id, after=compacted_through)
        active_msgs = [m for m in active if m.id != message_id]

        sys_msg = system_message(agent, summary)
        fixed = fixed_tokens(str(sys_msg.content), tool_schemas)
        room = room_tokens(agent.model, fixed)
        active_tokens = sum(message_len(m) for m in active_msgs) // 4
        trim_tools = active_tokens > room

        messages: list[BaseMessage] = [sys_msg]
        messages.extend(
            history_messages(
                active_msgs,
                current_turn_id=turn_id,
                trim_current_turn_tools=trim_tools,
            )
        )
        # A resolved stall ends here. The model node, which always runs next, clears it too on
        # success; this keeps the stall's lifetime within the node that set it.
        return {"messages": messages, "stall": None}

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

        # Floor check before taking inbound or calling model
        tool_schemas = [t.schema() for t in agent.tools]
        messages_list = state.get("messages", [])
        sys_text = str(messages_list[0].content) if messages_list else ""
        fixed = fixed_tokens(sys_text, tool_schemas)
        room = room_tokens(agent.model, fixed)
        if room < ROOM_FLOOR_TOKENS:
            budget = request_budget_tokens(agent.model)
            limit_val = (
                agent.model.request_token_limit
                if getattr(agent.model, "request_token_limit", None) is not None
                else getattr(agent.model, "context_window", None)
            )
            floor_text = (
                f"Cannot run {agent.definition.name} on {agent.model.address}: "
                f"room ({room} tokens) is below the minimum floor of {ROOM_FLOOR_TOKENS} tokens "
                f"(budget: {budget}, limit: {limit_val}, fixed part: {fixed} tokens). "
                f"Choose a model with a larger limit or context window."
            )
            floor_part = TextPart(type="text", text=floor_text)
            current_parts.append(floor_part)
            async with lock:
                await sink.text(message_id, floor_text)
                await store.update_message(project_id, message_id, parts=current_parts)
            return {
                "stopped_reason": "room_below_floor",
            }

        inbound = await hooks.inbound.take(project_id, session_id, turn_id)
        request = [*messages_list, *inbound]
        if end_note:
            request.append(HumanMessage(content=end_note))

        # The text of the step's current attempt: stored only when the step succeeds, or as the
        # partial answer of a failed or stopped step.
        streamed_chunks: list[str] = []

        async def on_text(delta: str) -> None:
            streamed_chunks.append(delta)
            async with lock:
                await sink.text(message_id, delta)

        async def take_back() -> None:
            """Take back the attempt's text: the stored message and its live copy (in the hub
            and the client) go back to the step's finished parts."""
            streamed_chunks.clear()
            async with lock:
                updated = await store.update_message(project_id, message_id, parts=current_parts)
                await sink.message(updated, created=False)

        retried_too_large = False
        while True:
            # Floor check before model request
            sys_text = str(request[0].content) if request else ""
            fixed = fixed_tokens(sys_text, tool_schemas)
            room = room_tokens(agent.model, fixed)
            if room < ROOM_FLOOR_TOKENS:
                budget = request_budget_tokens(agent.model)
                limit_val = (
                    agent.model.request_token_limit
                    if getattr(agent.model, "request_token_limit", None) is not None
                    else getattr(agent.model, "context_window", None)
                )
                floor_text = (
                    f"Cannot run {agent.definition.name} on {agent.model.address}: "
                    f"room ({room} tokens) is below the minimum floor of {ROOM_FLOOR_TOKENS} tokens "
                    f"(budget: {budget}, limit: {limit_val}, fixed part: {fixed} tokens). "
                    f"Choose a model with a larger limit or context window."
                )
                floor_part = TextPart(type="text", text=floor_text)
                current_parts.append(floor_part)
                async with lock:
                    await sink.text(message_id, floor_text)
                    await store.update_message(project_id, message_id, parts=current_parts)
                return {
                    "stopped_reason": "room_below_floor",
                }

            t0 = time.monotonic()
            try:
                reply = await caller.call(agent.model, request, tool_schemas, on_text, take_back)
                break
            except ProviderCallError as error:
                if error.kind is not ProviderErrorKind.REQUEST_TOO_LARGE or retried_too_large:
                    if streamed_chunks:
                        current_parts.append(TextPart(type="text", text="".join(streamed_chunks)))
                        await _update_parts()
                    msg = (
                        REQUEST_TOO_LARGE_MESSAGE
                        if error.kind is ProviderErrorKind.REQUEST_TOO_LARGE
                        else error.message
                    )
                    raise TurnFailed(msg) from error

                retried_too_large = True
                if streamed_chunks:
                    await take_back()

                # Compact the session (force compaction if turns exist before last 2; trim tool results of last 2 turns if needed)
                all_history = await store.messages(project_id, session_id)
                old_summary, compacted_through = await store.summary(project_id, session_id)
                uncompacted = [m for m in all_history if m.id != message_id]
                compacted_idx = -1
                if compacted_through:
                    for idx, m in enumerate(uncompacted):
                        if m.id == compacted_through:
                            compacted_idx = idx
                            break
                uncompacted_history = (
                    uncompacted[compacted_idx + 1 :] if compacted_idx >= 0 else uncompacted
                )
                uncompacted_turns = group_turns(uncompacted_history)

                compacted = False
                if len(uncompacted_turns) > 2:
                    try:
                        compacted = await compact_if_needed(
                            store,
                            caller,
                            agent.model,
                            project_id,
                            session_id,
                            uncompacted,
                            str(system_message(agent, old_summary).content),
                            turn_id=turn_id,
                            agent_name=agent.definition.name,
                            tools=tool_schemas,
                            force=True,
                        )
                    except ProviderCallError as comp_err:
                        if comp_err.kind is not ProviderErrorKind.REQUEST_TOO_LARGE:
                            raise
                        compacted = False

                summary, compacted_through = await store.summary(project_id, session_id)
                active = await store.messages(project_id, session_id, after=compacted_through)
                trimmed_history = history_messages(
                    [m for m in active if m.id != message_id],
                    current_turn_id=turn_id,
                    trim_current_turn_tools=True,
                )
                new_request = [system_message(agent, summary), *trimmed_history, *inbound]
                if end_note:
                    new_request.append(HumanMessage(content=end_note))

                # Check if new_request was compacted or trimmed compared to request
                old_req_chars = sum(len(str(m.content)) for m in request)
                new_req_chars = sum(len(str(m.content)) for m in new_request)
                if not compacted and new_req_chars >= old_req_chars:
                    raise TurnFailed(REQUEST_TOO_LARGE_MESSAGE) from error

                request = new_request
                continue
            except (Exception, asyncio.CancelledError) as error:
                if isinstance(error, ModelKeysExhausted):
                    # No attempt follows the last one, so its text is still shown.
                    if streamed_chunks:
                        await take_back()
                    stall = await record_stall(
                        hooks.approvals,
                        hooks.events,
                        project_id=project_id,
                        agent=agent.definition.name,
                        turn_id=turn_id,
                        step=step_count,
                        exhausted=error,
                        node="model",
                    )
                    # The step is taken again after Try again: no message and no step spent. The
                    # inbound messages are this turn's now (taken), so they stay in its state.
                    return {"messages": inbound, "stall": stall}
                # A failure with no next attempt, and a Stop, keep the text still shown.
                if streamed_chunks:
                    current_parts.append(TextPart(type="text", text="".join(streamed_chunks)))
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
            finish_reason = (reply.message.response_metadata or {}).get("finish_reason")
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
                if finish_reason == "length":
                    tool_errors[call_id] = (
                        "Reply cut by completion token limit. Write output in smaller parts."
                    )
                else:
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
            "stall": None,
        }

    initial_sys = str(system_message(agent, None).content)
    turn_fixed = fixed_tokens(initial_sys, [t.schema() for t in agent.tools])

    step = ToolStep(
        agent=agent,
        model=agent.model,
        fixed=turn_fixed,
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
        approval_store=hooks.approval_store,
    )

    async def turn_end_node(state: TurnState) -> dict[str, Any]:
        note = await hooks.turn_end.note(project_id, session_id, turn_id)
        return {"turn_end_checked": True, "end_note": note}

    async def wait_node(state: TurnState) -> dict[str, Any]:
        stall = pending_stall(state.get("stall"))
        if stall is None:
            return await step.wait(state)
        return await await_stall(
            stall,
            store=hooks.approval_store,
            caller=caller,
            events=hooks.events,
            project_id=project_id,
            agent=agent.definition.name,
            turn_id=turn_id,
        )

    def route_prepare(state: TurnState) -> str:
        return "wait" if pending_stall(state.get("stall")) is not None else "model"

    def route_model(state: TurnState) -> str:
        if state.get("stopped_reason"):
            return END
        if pending_stall(state.get("stall")) is not None:
            return "wait"
        last_msg = state["messages"][-1]
        if isinstance(last_msg, AIMessage) and bool(last_msg.tool_calls):
            return "decide"
        if not state.get("turn_end_checked"):
            return "turn_end"
        return END

    def route_decide(state: TurnState) -> str:
        return "wait" if needs_wait(state) else "tools"

    def route_wait(state: TurnState) -> str:
        # Only a resolved stall leaves the wait node with the stall still set: back to the node
        # that stalled.
        stall = state.get("stall")
        return stall.node if stall is not None else "tools"

    def route_turn_end(state: TurnState) -> str:
        return "model" if state.get("end_note") else END

    graph_builder = StateGraph(TurnState)
    graph_builder.add_node("prepare", prepare_node)  # pyright: ignore[reportUnknownMemberType]  # LangGraph add_node overload is partially untyped
    graph_builder.add_node("model", model_node)  # pyright: ignore[reportUnknownMemberType]  # LangGraph add_node overload is partially untyped
    graph_builder.add_node("decide", step.decide)  # pyright: ignore[reportUnknownMemberType]  # LangGraph add_node overload is partially untyped
    graph_builder.add_node("wait", wait_node)  # pyright: ignore[reportUnknownMemberType]  # LangGraph add_node overload is partially untyped
    graph_builder.add_node("tools", step.run)  # pyright: ignore[reportUnknownMemberType]  # LangGraph add_node overload is partially untyped
    graph_builder.add_node("turn_end", turn_end_node)  # pyright: ignore[reportUnknownMemberType]  # LangGraph add_node overload is partially untyped
    graph_builder.add_edge(START, "prepare")
    graph_builder.add_conditional_edges(
        "prepare", route_prepare, {"wait": "wait", "model": "model"}
    )
    graph_builder.add_conditional_edges(
        "model",
        route_model,
        {"wait": "wait", "decide": "decide", "turn_end": "turn_end", END: END},
    )
    graph_builder.add_conditional_edges("decide", route_decide, {"wait": "wait", "tools": "tools"})
    graph_builder.add_conditional_edges(
        "wait", route_wait, {"prepare": "prepare", "model": "model", "tools": "tools"}
    )
    graph_builder.add_edge("tools", "model")
    graph_builder.add_conditional_edges("turn_end", route_turn_end, {"model": "model", END: END})

    if checkpointer is not None:
        return graph_builder.compile(checkpointer=checkpointer)  # pyright: ignore[reportUnknownMemberType]  # LangGraph compile parameters are partially untyped
    return graph_builder.compile()  # pyright: ignore[reportUnknownMemberType]  # LangGraph compile return type is partially untyped
