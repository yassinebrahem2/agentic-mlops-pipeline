"""The decide, wait and tools nodes of the turn graph: the gate between the model and the tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.messages.tool import ToolCall

from mlops_agents.agents.runtime.agent import Agent
from mlops_agents.agents.runtime.contract import Part, ToolResultPart
from mlops_agents.agents.runtime.hooks import Gate, GateCall, GateDecision
from mlops_agents.agents.tasks.contract import TaskWorkspaces
from mlops_agents.agents.tools.base import (
    AgentTool,
    FileAccess,
    ToolContext,
    ToolFailure,
    ToolOutput,
)

if TYPE_CHECKING:
    from mlops_agents.agents.runtime.graph import TurnSink, TurnState

REJECTED_TEXT = "The user rejected this action."
DENIED_TEXT = "This action is not allowed."


def _calls(state: TurnState) -> tuple[list[ToolCall], dict[str, str]]:
    """The last model message's tool calls and the errors the model step already found."""
    last = state["messages"][-1]
    if not isinstance(last, AIMessage):
        return [], {}
    return list(last.tool_calls), dict(last.additional_kwargs.get("tool_errors") or {})


class ToolStep:
    """One turn's tool step. Holds the turn's file views, one per task, fetched when first used."""

    def __init__(
        self,
        *,
        agent: Agent,
        gate: Gate,
        workspaces: TaskWorkspaces,
        sink: TurnSink,
        project_id: str,
        session_id: str,
        turn_id: str,
        message_id: str,
        current_parts: list[Part],
        lock: AbstractAsyncContextManager[Any],
        update_parts: Callable[[], Awaitable[None]],
    ) -> None:
        self._agent = agent
        self._tools = {t.name: t for t in agent.tools}
        self._gate = gate
        self._workspaces = workspaces
        self._sink = sink
        self._project_id = project_id
        self._session_id = session_id
        self._turn_id = turn_id
        self._message_id = message_id
        self._parts = current_parts
        self._lock = lock
        self._update_parts = update_parts
        # The builder read the layout through the repository's view; its listing is reused.
        self._files: dict[str | None, FileAccess] = {None: agent.files}

    async def context(self) -> ToolContext:
        """The context as it stands now: the session's current task, else the repository."""
        task = await self._workspaces.current(self._project_id, self._session_id)
        key = None if task is None else task.id
        files = self._files.get(key)
        if files is None:
            files = await self._workspaces.files(self._project_id, task)
            self._files[key] = files
        return ToolContext(
            project_id=self._project_id,
            agent=self._agent.definition.name,
            session_id=self._session_id,
            turn_id=self._turn_id,
            files=files,
            task=task,
        )

    async def decide(self, state: TurnState) -> dict[str, Any]:
        """Answers the calls that cannot run, and asks the gate about the rest. Never pauses."""
        calls, errors = _calls(state)
        ctx = await self.context()
        gate_calls: list[GateCall] = []
        for call in calls:
            call_id = str(call["id"])
            if call_id in errors:
                continue
            tool = self._tools.get(call["name"])
            if tool is None:
                errors[call_id] = f"There is no tool named {call['name']}."
                continue
            args = tool.parse(call["args"])
            if isinstance(args, ToolOutput):
                errors[call_id] = args.content
                continue
            try:
                await tool.preview(ctx, args)
            except ToolFailure as exc:
                errors[call_id] = str(exc)
                continue
            gate_calls.append(
                GateCall(
                    tool_call_id=call_id,
                    message_id=self._message_id,
                    tool=tool,
                    args=args,
                    ctx=ctx,
                )
            )
        decisions = await self._gate.decide(gate_calls) if gate_calls else {}
        for gate_call in gate_calls:
            if gate_call.tool_call_id not in decisions:
                raise RuntimeError(
                    f"The gate returned no decision for the {gate_call.tool.name} call "
                    f"{gate_call.tool_call_id}; Gate.decide decides every call."
                )
        return {"decisions": decisions, "call_errors": errors}

    async def wait(self, state: TurnState) -> dict[str, Any]:
        decisions = state.get("decisions") or {}
        return {"decisions": await self._gate.wait(self._project_id, self._turn_id, decisions)}

    async def run(self, state: TurnState) -> dict[str, Any]:
        """Runs the allowed and approved calls one after another; answers the others."""
        calls, _ = _calls(state)
        errors = state.get("call_errors") or {}
        decisions = state.get("decisions") or {}
        tool_messages: list[BaseMessage] = []
        for call in calls:
            call_id = str(call["id"])
            if call_id in errors:
                output = ToolOutput(errors[call_id], is_error=True)
            else:
                output = await self._run_decided(
                    self._tools[call["name"]], call, decisions[call_id]
                )
            result = ToolResultPart(
                type="tool_result",
                tool_call_id=call_id,
                content=output.content,
                is_error=output.is_error,
            )
            async with self._lock:
                await self._sink.tool_finished(self._message_id, result)
            self._parts.append(result)
            tool_messages.append(ToolMessage(tool_call_id=call_id, content=result.content))

        await self._update_parts()
        return {"messages": tool_messages}

    async def _run_decided(
        self, tool: AgentTool[Any], call: ToolCall, decision: GateDecision
    ) -> ToolOutput:
        match decision.outcome:
            case "allow" | "approved":
                return await tool.invoke(await self.context(), call["args"])
            case "deny":
                return ToolOutput(decision.message or DENIED_TEXT, is_error=True)
            case "rejected":
                return ToolOutput(decision.message or REJECTED_TEXT, is_error=True)
            case "ask":
                raise RuntimeError(f"Tool call {call['id']} reached the tools step still waiting.")


def needs_wait(state: TurnState) -> bool:
    return any(d.outcome == "ask" for d in (state.get("decisions") or {}).values())
