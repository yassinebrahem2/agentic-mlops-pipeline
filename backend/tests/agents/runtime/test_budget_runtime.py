from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

from mlops_agents.agents.catalog import AgentDefinition, Limits
from mlops_agents.agents.runtime.agent import Agent
from mlops_agents.agents.runtime.contract import TextPart, ToolResultPart
from mlops_agents.agents.runtime.graph import create_turn_graph
from mlops_agents.agents.runtime.hooks import GateDecision
from mlops_agents.agents.runtime.model_caller import ModelCaller, ModelReply
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import AgentTool, TaskRef
from mlops_agents.agents.tools.bash import BashTool
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.sandbox.contract import CommandResult
from mlops_agents.storage.databases import Databases
from mlops_agents.workspace.models import Project
from tests.agents.runtime.graph_env import RecordingSink, graph_hooks
from tests.agents.tools.test_bash import FakeSandbox
from tests.conftest import make_repo


class AllowAllGate:
    async def decide(self, calls: Any) -> dict[str, GateDecision]:
        return {c.tool_call_id: GateDecision(outcome="allow") for c in calls}

    async def wait(self, project_id: str, turn_id: str, decisions: Any) -> dict[str, GateDecision]:
        return decisions


class MockModelCaller(ModelCaller):
    def __init__(self, replies: list[ModelReply]) -> None:
        self._replies = list(replies)
        self.call_count = 0
        self.last_messages: list[Any] = []

    async def call(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        model: ResolvedModel,
        messages: list[Any],
        tools: list[dict[str, Any]],
        on_text: Any,
        on_restart: Any = None,
    ) -> ModelReply:
        self.call_count += 1
        self.last_messages = list(messages)
        reply = self._replies.pop(0)
        if reply.message.content and isinstance(reply.message.content, str):
            res = on_text(reply.message.content)
            if asyncio.iscoroutine(res):
                await res
        return reply


def _make_custom_agent(
    repo_dir: Path,
    bin_dir: Path,
    *,
    fixed_prompt_len: int,
    request_limit: int | None,
    context_window: int = 64000,
    tools: tuple[str, ...] = ("Read",),
    extra_tools: list[AgentTool[Any]] | None = None,
) -> Agent:
    reg = default_registry()
    definition = AgentDefinition(
        name="test-agent",
        display_name="Test Agent",
        description="Budget test agent",
        tools=tools,
        chat=True,
        schedules=False,
        can_message=(),
        context_hooks=(),
        limits=Limits(max_steps=10),
        prompt="p" * fixed_prompt_len,
    )
    project = Project(
        id="prj_test",
        name="test",
        source="local",
        local_path=str(repo_dir),
        github_full_name=None,
        default_branch="main",
        status="ok",
        status_detail=None,
        created_at="2026-10-21T00:00:00Z",
        last_opened_at=None,
    )
    model = ResolvedModel(
        address="fake:fake-model",
        provider_id="fake",
        provider_name="fake",
        base_url="http://fake",
        wire_model_id="fake-model",
        context_window=context_window,
        keys=(),
        request_token_limit=request_limit,
    )
    agent_tools: list[AgentTool[Any]] = []
    for t in tools:
        tool_obj = reg.get(t)
        if tool_obj is not None:
            agent_tools.append(tool_obj)
    if extra_tools:
        agent_tools.extend(extra_tools)
    return Agent(
        definition=definition,
        project=project,
        layout="",
        model=model,
        max_steps=10,
        tools=tuple(agent_tools),
        files=RepoFiles(repo_dir, GitRunner(bin_dir)),
    )


async def test_room_floor_refuses_before_request(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "hi"}, git_env)
    # Fixed prompt ~23,600 chars -> ~5,900 tokens. Budget 8,000 -> reply reserve 1,200.
    # Room = 8,000 - 5,900 - 1,200 = 900 tokens < 1500 (ROOM_FLOOR_TOKENS).
    agent = _make_custom_agent(repo_dir, paths.bin, fixed_prompt_len=23600, request_limit=8000)
    store = SessionStore(databases)
    sink = RecordingSink()
    caller = MockModelCaller([])

    session_id = await store.session_id("prj_test", agent.definition.name)
    user_msg = await store.add_message(
        "prj_test",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Hello")],
        status="complete",
    )
    turn_id = await store.add_turn("prj_test", session_id, user_msg.id, "running")
    agent_msg = await store.add_message(
        "prj_test",
        session_id,
        turn_id=turn_id,
        author_kind="agent",
        author_name=agent.definition.name,
        parts=[],
        status="streaming",
    )

    current_parts: list[Any] = []
    graph = create_turn_graph(
        agent=agent,
        caller=caller,
        store=store,
        sink=sink,
        project_id="prj_test",
        session_id=session_id,
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=current_parts,
        lock=asyncio.Lock(),
        hooks=graph_hooks(agent, databases),
    )

    result = await graph.ainvoke({"messages": [], "step_count": 0, "stopped_reason": None})  # pyright: ignore[reportUnknownMemberType]
    # Must refuse without sending request
    assert caller.call_count == 0
    assert result.get("stopped_reason") == "room_below_floor"
    assert len(current_parts) == 1
    text_content = current_parts[0].text
    assert "room" in text_content
    assert "floor" in text_content
    assert agent.definition.name in text_content
    assert agent.model.address in text_content
    assert "8000" in text_content


async def test_runs_at_250k_budget(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "hi"}, git_env)
    # Fixed prompt ~23,600 chars -> ~5,900 tokens. Budget 250,000 -> runs.
    agent = _make_custom_agent(
        repo_dir, paths.bin, fixed_prompt_len=23600, request_limit=None, context_window=250000
    )
    store = SessionStore(databases)
    sink = RecordingSink()
    caller = MockModelCaller(
        [ModelReply(message=AIMessage(content="All good!"), prompt_tokens=10, completion_tokens=5)]
    )

    session_id = await store.session_id("prj_test", agent.definition.name)
    user_msg = await store.add_message(
        "prj_test",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Hello")],
        status="complete",
    )
    turn_id = await store.add_turn("prj_test", session_id, user_msg.id, "running")
    agent_msg = await store.add_message(
        "prj_test",
        session_id,
        turn_id=turn_id,
        author_kind="agent",
        author_name=agent.definition.name,
        parts=[],
        status="streaming",
    )

    current_parts: list[Any] = []
    graph = create_turn_graph(
        agent=agent,
        caller=caller,
        store=store,
        sink=sink,
        project_id="prj_test",
        session_id=session_id,
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=current_parts,
        lock=asyncio.Lock(),
        hooks=graph_hooks(agent, databases),
    )

    result = await graph.ainvoke({"messages": [], "step_count": 0, "stopped_reason": None})  # pyright: ignore[reportUnknownMemberType]
    assert caller.call_count == 1
    assert result.get("stopped_reason") is None


async def test_failure_case_on_main_read_exceeding_cap_returns_tool_error(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    # 2,750 fixed at 8,000 budget: reply reserve 1,200 -> room = 4,050.
    # tool_result_max_chars = 4,050.
    # Create a file of 6,000 characters > 4,050 cap.
    big_content = "line in file\n" * 500
    repo_dir = make_repo(tmp_path / "repo", {"big.txt": big_content}, git_env)
    agent = _make_custom_agent(
        repo_dir, paths.bin, fixed_prompt_len=11000, request_limit=8000, tools=("Read",)
    )
    store = SessionStore(databases)
    sink = RecordingSink()

    # Step 1: Model calls Read on big.txt whole file
    call_1 = ModelReply(
        message=AIMessage(
            content="",
            tool_calls=[{"id": "call_read", "name": "Read", "args": {"file_path": "big.txt"}}],
        ),
        prompt_tokens=10,
        completion_tokens=5,
    )
    # Step 2: Model receives tool result and responds
    call_2 = ModelReply(
        message=AIMessage(content="Saw tool result"),
        prompt_tokens=20,
        completion_tokens=5,
    )
    caller = MockModelCaller([call_1, call_2])

    session_id = await store.session_id("prj_test", agent.definition.name)
    user_msg = await store.add_message(
        "prj_test",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Read big file")],
        status="complete",
    )
    turn_id = await store.add_turn("prj_test", session_id, user_msg.id, "running")
    agent_msg = await store.add_message(
        "prj_test",
        session_id,
        turn_id=turn_id,
        author_kind="agent",
        author_name=agent.definition.name,
        parts=[],
        status="streaming",
    )

    current_parts: list[Any] = []
    graph = create_turn_graph(
        agent=agent,
        caller=caller,
        store=store,
        sink=sink,
        project_id="prj_test",
        session_id=session_id,
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=current_parts,
        lock=asyncio.Lock(),
        hooks=graph_hooks(agent, databases),
    )

    await graph.ainvoke({"messages": [], "step_count": 0, "stopped_reason": None})  # pyright: ignore[reportUnknownMemberType]
    assert caller.call_count == 2
    # Verify what model got in step 2: it got the tool error, not the file!
    last_msgs = caller.last_messages
    tool_msg = next(m for m in last_msgs if getattr(m, "tool_call_id", None) == "call_read")
    assert "too large to read whole" in tool_msg.content
    assert "offset and limit" in tool_msg.content
    assert "Grep" in tool_msg.content


async def test_tool_output_over_budget_cut_once_in_stored_and_model(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    # Set up agent with Grep tool
    repo_dir = make_repo(tmp_path / "repo", {"src/app.py": "x\n" * 1000}, git_env)
    agent = _make_custom_agent(
        repo_dir, paths.bin, fixed_prompt_len=11000, request_limit=8000, tools=("Grep",)
    )
    store = SessionStore(databases)
    sink = RecordingSink()

    call_1 = ModelReply(
        message=AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "c_grep",
                    "name": "Grep",
                    "args": {"pattern": "x", "output_mode": "content"},
                }
            ],
        ),
        prompt_tokens=10,
        completion_tokens=5,
    )
    call_2 = ModelReply(
        message=AIMessage(content="Grep done"),
        prompt_tokens=15,
        completion_tokens=5,
    )
    caller = MockModelCaller([call_1, call_2])

    session_id = await store.session_id("prj_test", agent.definition.name)
    user_msg = await store.add_message(
        "prj_test",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Grep search")],
        status="complete",
    )
    turn_id = await store.add_turn("prj_test", session_id, user_msg.id, "running")
    agent_msg = await store.add_message(
        "prj_test",
        session_id,
        turn_id=turn_id,
        author_kind="agent",
        author_name=agent.definition.name,
        parts=[],
        status="streaming",
    )

    current_parts: list[Any] = []
    graph = create_turn_graph(
        agent=agent,
        caller=caller,
        store=store,
        sink=sink,
        project_id="prj_test",
        session_id=session_id,
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=current_parts,
        lock=asyncio.Lock(),
        hooks=graph_hooks(agent, databases),
    )

    await graph.ainvoke({"messages": [], "step_count": 0, "stopped_reason": None})  # pyright: ignore[reportUnknownMemberType]
    assert caller.call_count == 2
    # Verify ToolResultPart in current_parts
    tool_part = next(p for p in current_parts if isinstance(p, ToolResultPart))
    assert "characters cut to fit context budget" in tool_part.content
    # Notice must appear only ONCE
    assert tool_part.content.count("characters cut to fit context budget") == 1

    # Verify what model got
    last_msgs = caller.last_messages
    tool_msg = next(m for m in last_msgs if getattr(m, "tool_call_id", None) == "c_grep")
    assert tool_msg.content == tool_part.content


async def test_bash_output_over_budget_cut_once_in_stored_and_model(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"src/app.py": "x\n"}, git_env)
    bash_tool = BashTool(
        sandbox=FakeSandbox(
            CommandResult(output="y" * 10000, exit_code=0, timed_out=False, truncated=False)
        )
    )
    agent = _make_custom_agent(
        repo_dir,
        paths.bin,
        fixed_prompt_len=11000,
        request_limit=8000,
        tools=("Bash",),
        extra_tools=[bash_tool],
    )
    store = SessionStore(databases)
    sink = RecordingSink()

    call_1 = ModelReply(
        message=AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "c_bash",
                    "name": "Bash",
                    "args": {"command": "python -c \"print('y' * 10000)\""},
                }
            ],
        ),
        prompt_tokens=10,
        completion_tokens=5,
    )
    call_2 = ModelReply(
        message=AIMessage(content="Bash done"),
        prompt_tokens=15,
        completion_tokens=5,
    )
    caller = MockModelCaller([call_1, call_2])

    session_id = await store.session_id("prj_test", agent.definition.name)
    user_msg = await store.add_message(
        "prj_test",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Run bash")],
        status="complete",
    )
    turn_id = await store.add_turn("prj_test", session_id, user_msg.id, "running")
    agent_msg = await store.add_message(
        "prj_test",
        session_id,
        turn_id=turn_id,
        author_kind="agent",
        author_name=agent.definition.name,
        parts=[],
        status="streaming",
    )

    task = TaskRef(id="tsk_1", title="Task 1", branch="task-1", worktree=repo_dir, status="active")

    class WorkspacesWithTask:
        async def current(self, project_id: str, session_id: str) -> TaskRef | None:
            return task

        async def files(self, project_id: str, task: TaskRef | None) -> Any:
            return agent.files

        async def view(self, project_id: str, task_id: str) -> Any: ...

    current_parts: list[Any] = []
    base_hooks = graph_hooks(agent, databases)
    hooks = replace(base_hooks, gate=AllowAllGate(), workspaces=WorkspacesWithTask())
    graph = create_turn_graph(
        agent=agent,
        caller=caller,
        store=store,
        sink=sink,
        project_id="prj_test",
        session_id=session_id,
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=current_parts,
        lock=asyncio.Lock(),
        hooks=hooks,
    )

    await graph.ainvoke({"messages": [], "step_count": 0, "stopped_reason": None})  # pyright: ignore[reportUnknownMemberType]
    assert caller.call_count == 2
    tool_part = next(p for p in current_parts if isinstance(p, ToolResultPart))
    assert "characters cut to fit context budget" in tool_part.content
    assert tool_part.content.count("characters cut to fit context budget") == 1

    last_msgs = caller.last_messages
    tool_msg = next(m for m in last_msgs if getattr(m, "tool_call_id", None) == "c_bash")
    assert tool_msg.content == tool_part.content
