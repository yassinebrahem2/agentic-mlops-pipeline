from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.agent import Agent
from mlops_agents.agents.runtime.contract import MessageView, TextPart, ToolCallPart, ToolResultPart
from mlops_agents.agents.runtime.graph import TurnState, create_turn_graph
from mlops_agents.agents.runtime.model_caller import ModelCaller, ModelReply
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import ToolContext
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.storage.databases import Databases
from mlops_agents.workspace.models import Project
from tests.conftest import make_repo


class RecordingSink:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.tools_started: list[ToolCallPart] = []
        self.tools_finished: list[ToolResultPart] = []
        self.messages: list[tuple[MessageView, bool]] = []

    async def text(self, message_id: str, text: str) -> None:
        self.texts.append(text)

    async def tool_started(self, message_id: str, call: ToolCallPart) -> None:
        self.tools_started.append(call)

    async def tool_finished(self, message_id: str, result: ToolResultPart) -> None:
        self.tools_finished.append(result)

    async def message(self, message: MessageView, *, created: bool) -> None:
        self.messages.append((message, created))


def _make_agent(repo_dir: Path, bin_dir: Path, tool_names: tuple[str, ...] | None = None) -> Agent:
    tools = default_registry()
    catalog = AgentCatalog.load(tools.names())
    definition = catalog.get("orchestrator")
    project = Project(
        id="prj_1",
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
        address="fake:fake-tools",
        provider_id="fake",
        provider_name="fake",
        base_url="http://fake",
        wire_model_id="fake-tools",
        context_window=32768,
        keys=(),
    )
    repo_files = RepoFiles(repo_dir, GitRunner(bin_dir))
    tool_ctx = ToolContext(files=repo_files)
    agent_tools: list[Any] = []
    allowed_names = tool_names if tool_names is not None else tuple(definition.tools)
    for name in allowed_names:
        t = tools.get(name)
        if t is not None:
            agent_tools.append(t)

    return Agent(
        definition=definition,
        project=project,
        layout="README.md",
        model=model,
        max_steps=25,
        tools=tuple(agent_tools),
        tool_context=tool_ctx,
    )


class MockModelCaller(ModelCaller):
    def __init__(self, replies: list[ModelReply]) -> None:
        self._replies = list(replies)
        self.call_count = 0

    async def call(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        model: ResolvedModel,
        messages: list[Any],
        tools: list[dict[str, Any]],
        on_text: Any,
    ) -> ModelReply:
        self.call_count += 1
        reply = self._replies.pop(0)
        if reply.message.content and isinstance(reply.message.content, str):
            res = on_text(reply.message.content)
            if asyncio.iscoroutine(res):
                await res
        return reply


async def test_graph_tool_not_in_definition_returns_error(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "Line 1\n"}, git_env)
    # Agent only has tool "Write", NOT "Read"
    agent = _make_agent(repo_dir, paths.bin, tool_names=("Write",))
    store = SessionStore(databases)
    sink = RecordingSink()

    session_id = await store.session_id("prj_1", "orchestrator")
    user_msg = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Read file")],
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

    caller = MockModelCaller(
        [
            ModelReply(
                message=AIMessage(
                    content="",
                    tool_calls=[{"id": "c1", "name": "Read", "args": {"file_path": "README.md"}}],
                ),
                prompt_tokens=10,
                completion_tokens=5,
            ),
            ModelReply(
                message=AIMessage(content="Tool was unavailable", tool_calls=[]),
                prompt_tokens=15,
                completion_tokens=4,
            ),
        ]
    )

    current_parts: list[Any] = []
    graph = create_turn_graph(
        agent=agent,
        caller=caller,
        store=store,
        sink=sink,
        project_id="prj_1",
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=current_parts,
    )

    initial_state: TurnState = {
        "messages": [HumanMessage(content="Read file")],
        "step_count": 0,
        "stopped_reason": None,
    }
    await graph.ainvoke(initial_state)  # pyright: ignore[reportUnknownMemberType]

    assert caller.call_count == 2
    assert len(sink.tools_started) == 1
    assert sink.tools_started[0].name == "Read"
    assert len(sink.tools_finished) == 1
    assert sink.tools_finished[0].is_error is True
    assert sink.tools_finished[0].content == "There is no tool named Read."

    # Parts order: tool_call, tool_result, text
    types = [p.type for p in current_parts]
    assert types == ["tool_call", "tool_result", "text"]
    assert current_parts[1].content == "There is no tool named Read."


async def test_graph_parallel_tools_order(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"a.txt": "A\n", "b.txt": "B\n"}, git_env)
    agent = _make_agent(repo_dir, paths.bin, tool_names=("Read",))
    store = SessionStore(databases)
    sink = RecordingSink()

    session_id = await store.session_id("prj_1", "orchestrator")
    user_msg = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Read both")],
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

    caller = MockModelCaller(
        [
            ModelReply(
                message=AIMessage(
                    content="",
                    tool_calls=[
                        {"id": "c1", "name": "Read", "args": {"file_path": "a.txt"}},
                        {"id": "c2", "name": "Read", "args": {"file_path": "b.txt"}},
                    ],
                ),
                prompt_tokens=10,
                completion_tokens=5,
            ),
            ModelReply(
                message=AIMessage(content="Both read", tool_calls=[]),
                prompt_tokens=15,
                completion_tokens=4,
            ),
        ]
    )

    current_parts: list[Any] = []
    graph = create_turn_graph(
        agent=agent,
        caller=caller,
        store=store,
        sink=sink,
        project_id="prj_1",
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=current_parts,
    )

    initial_state: TurnState = {
        "messages": [HumanMessage(content="Read both")],
        "step_count": 0,
        "stopped_reason": None,
    }
    await graph.ainvoke(initial_state)  # pyright: ignore[reportUnknownMemberType]

    # Stored parts order: call1, call2, result1, result2, text
    types = [p.type for p in current_parts]
    assert types == ["tool_call", "tool_call", "tool_result", "tool_result", "text"]

    # Live events order: call1, call2 started, then result1, result2 finished
    assert len(sink.tools_started) == 2
    assert [c.id for c in sink.tools_started] == ["c1", "c2"]
    assert len(sink.tools_finished) == 2
    assert [r.tool_call_id for r in sink.tools_finished] == ["c1", "c2"]
