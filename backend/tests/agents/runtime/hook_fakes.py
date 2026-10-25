"""Test doubles for the turn hooks, plus a graph run that records every model request."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.messages.tool import ToolCall
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel

from mlops_agents.agents.runtime.agent import Agent
from mlops_agents.agents.runtime.contract import Part, TextPart
from mlops_agents.agents.runtime.graph import TurnHooks, TurnState, create_turn_graph
from mlops_agents.agents.runtime.hooks import (
    Gate,
    GateCall,
    GateDecision,
    InboundSource,
    NoInbound,
    NoTurnEndCheck,
    TurnEndCheck,
)
from mlops_agents.agents.runtime.hub import AsyncRLock
from mlops_agents.agents.runtime.model_caller import ModelCaller, ModelReply
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.tasks.contract import TaskWorkspaces
from mlops_agents.agents.tools.base import (
    AgentTool,
    FileAccess,
    Preview,
    RiskClass,
    TaskRef,
    ToolContext,
    ToolFailure,
    ToolOutput,
)
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.storage.databases import Databases

ALLOW = GateDecision(outcome="allow", message=None, approval_id=None, preview=None)
APPROVED = GateDecision(outcome="approved", message=None, approval_id="apr_1", preview=None)


class AllowAllGate(Gate):
    """Test fake gate that allows every call."""

    async def decide(self, calls: Sequence[GateCall]) -> dict[str, GateDecision]:
        allow = GateDecision(outcome="allow", message=None, approval_id=None, preview=None)
        return {call.tool_call_id: allow for call in calls}

    async def wait(
        self, project_id: str, turn_id: str, decisions: dict[str, GateDecision]
    ) -> dict[str, GateDecision]:
        return decisions


def ask(approval_id: str) -> GateDecision:
    preview = Preview(kind="text", title="Echo", body="{}")
    return GateDecision(outcome="ask", message=None, approval_id=approval_id, preview=preview)


def deny(message: str) -> GateDecision:
    return GateDecision(outcome="deny", message=message, approval_id=None, preview=None)


class EchoArgs(BaseModel):
    text: str


class EchoTool(AgentTool[EchoArgs]):
    name = "Echo"
    description = "Echoes its text."
    risk = RiskClass.READ
    args_model = EchoArgs

    def __init__(self, after_run: Callable[[], None] | None = None) -> None:
        self.contexts: list[ToolContext] = []
        self.ran: list[str] = []
        self._after_run = after_run

    async def run(self, ctx: ToolContext, args: EchoArgs) -> ToolOutput:
        self.contexts.append(ctx)
        self.ran.append(args.text)
        if self._after_run is not None:
            self._after_run()
        return ToolOutput(f"echo {args.text}")


class NoPreviewTool(AgentTool[EchoArgs]):
    """A tool whose preview fails, like an Edit of a missing file."""

    name = "NoPreview"
    description = "Its preview raises."
    risk = RiskClass.WRITE_CODE
    args_model = EchoArgs

    async def preview(self, ctx: ToolContext, args: EchoArgs) -> Preview:
        raise ToolFailure(f"There is no file {args.text}.")

    async def run(self, ctx: ToolContext, args: EchoArgs) -> ToolOutput:
        raise AssertionError("a call whose preview fails never runs")


class ScriptedGate:
    """Decides from a table (allow by default); `wait` approves every ask."""

    def __init__(
        self, outcomes: dict[str, GateDecision] | None = None, approve_as: GateDecision = APPROVED
    ) -> None:
        self._outcomes = outcomes or {}
        self._approved = approve_as
        self.decided: list[list[GateCall]] = []
        self.waited: list[dict[str, GateDecision]] = []

    async def decide(self, calls: Sequence[GateCall]) -> dict[str, GateDecision]:
        self.decided.append(list(calls))
        return {c.tool_call_id: self._outcomes.get(c.tool_call_id, ALLOW) for c in calls}

    async def wait(
        self, project_id: str, turn_id: str, decisions: dict[str, GateDecision]
    ) -> dict[str, GateDecision]:
        self.waited.append(dict(decisions))
        return {cid: self._approved if d.outcome == "ask" else d for cid, d in decisions.items()}


class ScriptedInbound:
    """Hands out one batch per `take`, then nothing."""

    def __init__(self, batches: list[list[BaseMessage]]) -> None:
        self._batches = list(batches)
        self.takes = 0

    async def take(self, project_id: str, session_id: str, turn_id: str) -> list[BaseMessage]:
        self.takes += 1
        return self._batches.pop(0) if self._batches else []


class ScriptedTurnEnd:
    def __init__(self, note: str | None) -> None:
        self._note = note
        self.asked = 0

    async def note(self, project_id: str, session_id: str, turn_id: str) -> str | None:
        self.asked += 1
        return self._note


class SwitchingWorkspaces:
    """Its current task is whatever the test sets; counts the views it hands out."""

    def __init__(self, repository: FileAccess, task_files: FileAccess) -> None:
        self.task: TaskRef | None = None
        self._repository = repository
        self._task_files = task_files
        self.files_calls: list[TaskRef | None] = []

    async def current(self, project_id: str, session_id: str) -> TaskRef | None:
        return self.task

    async def files(self, project_id: str, task: TaskRef | None) -> FileAccess:
        self.files_calls.append(task)
        return self._repository if task is None else self._task_files


class RecordingCaller(ModelCaller):
    """Replays replies and keeps a copy of every request's messages."""

    def __init__(self, replies: list[ModelReply]) -> None:
        self._replies = list(replies)
        self.requests: list[list[BaseMessage]] = []

    async def call(
        self,
        model: ResolvedModel,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]],
        on_text: Callable[[str], Awaitable[None]],
    ) -> ModelReply:
        self.requests.append(list(messages))
        return self._replies.pop(0)


def tool_reply(*calls: ToolCall) -> ModelReply:
    return ModelReply(
        message=AIMessage(content="", tool_calls=list(calls)), prompt_tokens=1, completion_tokens=1
    )


def text_reply(text: str) -> ModelReply:
    return ModelReply(message=AIMessage(content=text), prompt_tokens=1, completion_tokens=1)


def echo(call_id: str, text: str, name: str = "Echo") -> ToolCall:
    return {"id": call_id, "name": name, "args": {"text": text}, "type": "tool_call"}


@dataclass
class GraphRun:
    agent: Agent
    caller: RecordingCaller
    sink: Any
    parts: list[Part]
    state: dict[str, Any]
    message_id: str
    session_id: str
    turn_id: str


async def run_graph(
    tmp_path: Path,
    bin_dir: Path,
    databases: Databases,
    replies: list[ModelReply],
    *,
    tools: tuple[AgentTool[Any], ...],
    gate: Gate | None = None,
    inbound: InboundSource | None = None,
    turn_end: TurnEndCheck | None = None,
    workspaces: TaskWorkspaces | None = None,
    max_steps: int | None = 25,
    checkpointer: AsyncSqliteSaver | None = None,
) -> GraphRun:
    """One turn of the graph over a plain directory, with the given hooks (stage 1's elsewhere)."""
    from tests.agents.runtime.graph_env import FixedWorkspaces, RecordingSink, make_agent
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    agent = replace(
        make_agent(repo_dir, bin_dir, project_id="prj_1", max_steps=max_steps), tools=tools
    )
    store = SessionStore(databases)
    session_id = await store.session_id("prj_1", "orchestrator")
    user_msg = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Go")],
        status="complete",
    )
    turn_id = await store.add_turn("prj_1", session_id, user_msg.id, "running")
    agent_msg = await store.add_message(
        "prj_1",
        session_id,
        turn_id=turn_id,
        author_kind="agent",
        author_name="orchestrator",
        parts=[],
        status="streaming",
    )
    caller = RecordingCaller(replies)
    sink = RecordingSink()
    parts: list[Part] = []
    hooks = TurnHooks(
        gate=gate or AllowAllGate(),
        inbound=inbound or NoInbound(),
        turn_end=turn_end or NoTurnEndCheck(),
        workspaces=workspaces or FixedWorkspaces(agent.files),
    )
    graph = create_turn_graph(
        agent=agent,
        caller=caller,
        store=store,
        sink=sink,
        project_id="prj_1",
        session_id=session_id,
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=parts,
        lock=AsyncRLock(),
        hooks=hooks,
        checkpointer=checkpointer,
    )
    initial: TurnState = {
        "messages": [SystemMessage(content="You are a test agent."), HumanMessage(content="Go")],
        "step_count": 0,
        "stopped_reason": None,
    }
    config: RunnableConfig = {"configurable": {"thread_id": turn_id}}
    state = await asyncio.wait_for(graph.ainvoke(initial, config=config), timeout=10)  # pyright: ignore[reportUnknownMemberType]  # LangGraph ainvoke is partially untyped
    return GraphRun(
        agent=agent,
        caller=caller,
        sink=sink,
        parts=parts,
        state=dict(state),  # pyright: ignore[reportUnknownArgumentType]  # LangGraph state is untyped
        message_id=agent_msg.id,
        session_id=session_id,
        turn_id=turn_id,
    )
