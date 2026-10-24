from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx2
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.agent import AgentBuilder
from mlops_agents.agents.runtime.contract import TextPart
from mlops_agents.agents.runtime.executor import TurnExecutor
from mlops_agents.agents.runtime.graph import (
    TurnState,
    create_turn_graph,
    format_invalid_tool_call_error,
)
from mlops_agents.agents.runtime.hub import AsyncRLock
from mlops_agents.agents.runtime.model_caller import ModelCaller
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools import default_registry
from mlops_agents.config import RunMode, load_settings
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.workspace.service import WorkspaceService
from tests.agents.runtime.graph_env import RecordingSink, make_agent
from tests.agents.runtime.test_graph_tools import MockModelCaller, ModelReply
from tests.conftest import MemoryKeyStore, make_repo
from tests.fakes.fake_openai import RunningFake
from tests.integrations.test_github_auth import FakeGh


@dataclass
class _ExecutorEnv:
    executor: TurnExecutor
    settings: AgentSettingsService
    providers: ProvidersService
    project_id: str
    session_id: str
    turn_id: str


async def _setup_executor_env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> _ExecutorEnv:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "Line 1 of readme\nLine 2\n"}, git_env)
    settings = replace(
        load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)}),
        mode=RunMode.LOCAL,
    )
    tools = default_registry()
    catalog = AgentCatalog.load(tools.names())
    providers = ProvidersService(databases, keystore, model_http)
    agent_settings = AgentSettingsService(databases, providers, catalog)
    github = GitHubAuth(databases, keystore, httpx2.AsyncClient(), FakeGh(None))
    workspace = WorkspaceService(
        settings, paths, databases, GitRunner(paths.bin, github.token), github, httpx2.AsyncClient()
    )
    builder = AgentBuilder(
        catalog=catalog,
        tools=tools,
        settings=agent_settings,
        workspace=workspace,
        git=GitRunner(paths.bin),
    )
    store = SessionStore(databases)
    executor = TurnExecutor(store=store, caller=ModelCaller(providers, model_http), builder=builder)

    prov = await providers.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake.base_url)
    )
    await providers.add_model(ModelCreate(provider=prov.name, wire_model_id="fake-tools"))
    await providers.verify_model(f"{prov.name}:fake-tools")

    project = await workspace.add_local(str(repo_dir))
    await agent_settings.set_default_model("orchestrator", f"{prov.name}:fake-tools")
    session_id = await store.session_id(project.id, "orchestrator")

    user_msg = await store.add_message(
        project.id,
        session_id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Check readme")],
        status="complete",
    )
    turn_id = await store.add_turn(project.id, session_id, user_msg.id, "running")

    return _ExecutorEnv(
        executor=executor,
        settings=agent_settings,
        providers=providers,
        project_id=project.id,
        session_id=session_id,
        turn_id=turn_id,
    )


def test_format_invalid_tool_call_error_caps_at_500() -> None:
    long_raw = "x" * 600
    msg = format_invalid_tool_call_error("my_tool", long_raw)
    assert msg == f"The arguments of my_tool were not valid JSON: {'x' * 500}"
    assert len(msg) == len("The arguments of my_tool were not valid JSON: ") + 500


def test_format_invalid_tool_call_error_handles_none_or_empty() -> None:
    assert (
        format_invalid_tool_call_error("tool", None)
        == "The arguments of tool were not valid JSON: "
    )
    assert (
        format_invalid_tool_call_error("tool", "{pattern: foo}")
        == "The arguments of tool were not valid JSON: {pattern: foo}"
    )


async def test_invalid_tool_call_turn_shows_error_and_continues(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env = await _setup_executor_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    await env.providers.add_model(ModelCreate(provider="fake", wire_model_id="fake-invalid-tools"))
    await env.providers.verify_model("fake:fake-invalid-tools")
    await env.settings.set_default_model("orchestrator", "fake:fake-invalid-tools")

    fake_openai.state.requests.clear()
    sink = RecordingSink()
    end_status, error_text = await env.executor.run(
        env.project_id, env.session_id, "orchestrator", env.turn_id, sink, AsyncRLock()
    )

    assert end_status == "done"
    assert error_text is None

    assert len(sink.tools_started) == 1
    call_part = sink.tools_started[0]
    assert call_part.name == "Read"
    assert call_part.input == {}

    assert len(sink.tools_finished) == 1
    result_part = sink.tools_finished[0]
    assert result_part.tool_call_id == call_part.id
    assert result_part.is_error is True
    assert result_part.content == "The arguments of Read were not valid JSON: {pattern: foo}"

    # Fake must have received 2 requests
    assert len(fake_openai.state.requests) == 2
    req2_messages = fake_openai.state.requests[1]["messages"]

    assistant_msgs = [m for m in req2_messages if m.get("role") == "assistant"]
    assert len(assistant_msgs) == 1
    assistant_tool_calls = assistant_msgs[0].get("tool_calls")
    assert assistant_tool_calls is not None
    assert len(assistant_tool_calls) == 1
    assert assistant_tool_calls[0]["id"] == call_part.id
    assert assistant_tool_calls[0]["function"]["arguments"] == "{}"

    tool_msgs = [m for m in req2_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == call_part.id
    assert tool_msgs[0]["content"] == "The arguments of Read were not valid JSON: {pattern: foo}"

    assert "Done. First line:" in "".join(sink.texts)


@pytest.mark.parametrize("first_valid", [True, False])
async def test_mixed_valid_and_invalid_tool_calls_order(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    first_valid: bool,
) -> None:
    env = await _setup_executor_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    valid_call = {
        "id": "call_valid_alpha",
        "name": "Read",
        "arguments": {"file_path": "README.md"},
    }
    invalid_call = {
        "id": "call_invalid_omega",
        "name": "Read",
        "arguments": "{pattern: foo}",
    }
    fake_openai.state.scripted_tool_calls = (
        [valid_call, invalid_call] if first_valid else [invalid_call, valid_call]
    )
    fake_openai.state.requests.clear()

    sink = RecordingSink()
    end_status, error_text = await env.executor.run(
        env.project_id, env.session_id, "orchestrator", env.turn_id, sink, AsyncRLock()
    )

    assert end_status == "done"
    assert error_text is None

    assert len(sink.tools_started) == 2
    first_input = {"file_path": "README.md"} if first_valid else {}
    second_input = {} if first_valid else {"file_path": "README.md"}
    assert sink.tools_started[0].input == first_input
    assert sink.tools_started[1].input == second_input

    assert len(sink.tools_finished) == 2
    assert sink.tools_finished[0].is_error is not first_valid
    assert sink.tools_finished[1].is_error is first_valid
    if first_valid:
        assert "Line 1 of readme" in sink.tools_finished[0].content
        assert (
            sink.tools_finished[1].content
            == "The arguments of Read were not valid JSON: {pattern: foo}"
        )
    else:
        assert (
            sink.tools_finished[0].content
            == "The arguments of Read were not valid JSON: {pattern: foo}"
        )
        assert "Line 1 of readme" in sink.tools_finished[1].content

    # Second request has assistant message with both calls in order, followed by tool messages in order
    assert len(fake_openai.state.requests) == 2
    req2_messages = fake_openai.state.requests[1]["messages"]
    assistant_msg = next(m for m in req2_messages if m.get("role") == "assistant")
    tool_msgs = [m for m in req2_messages if m.get("role") == "tool"]

    assert len(assistant_msg["tool_calls"]) == 2
    assert len(tool_msgs) == 2
    assert assistant_msg["tool_calls"][0]["id"] == tool_msgs[0]["tool_call_id"]
    assert assistant_msg["tool_calls"][1]["id"] == tool_msgs[1]["tool_call_id"]
    invalid_idx = 1 if first_valid else 0
    assert assistant_msg["tool_calls"][invalid_idx]["function"]["arguments"] == "{}"


async def test_graph_invalid_tool_call_with_mock_caller(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "Line 1\n"}, git_env)
    agent = make_agent(repo_dir, paths.bin, project_id="prj_1", tool_names=("Read",))
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
                    tool_calls=[],
                    invalid_tool_calls=[
                        {
                            "type": "invalid_tool_call",
                            "id": "c1",
                            "name": "Read",
                            "args": "{broken",
                            "error": None,
                        }
                    ],
                ),
                prompt_tokens=10,
                completion_tokens=5,
            ),
            ModelReply(
                message=AIMessage(content="Recovered from bad JSON", tool_calls=[]),
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
        lock=AsyncRLock(),
    )

    initial_state: TurnState = {
        "messages": [HumanMessage(content="Read file")],
        "step_count": 0,
        "stopped_reason": None,
    }
    state = await graph.ainvoke(initial_state)  # pyright: ignore[reportUnknownMemberType]

    assert state.get("stopped_reason") is None
    assert state.get("step_count") == 2
    assert len(sink.tools_started) == 1
    assert sink.tools_started[0].input == {}
    assert len(sink.tools_finished) == 1
    assert sink.tools_finished[0].is_error is True
    assert sink.tools_finished[0].content == "The arguments of Read were not valid JSON: {broken"

    # In state messages, the assistant message has tool_calls with args: {} and invalid_tool_calls: []
    messages = state["messages"]
    ai_msg_with_call = next(m for m in messages if isinstance(m, AIMessage) and m.tool_calls)
    assert len(ai_msg_with_call.tool_calls) == 1
    assert ai_msg_with_call.tool_calls[0]["args"] == {}
    assert ai_msg_with_call.tool_calls[0]["id"] == "c1"
    assert ai_msg_with_call.invalid_tool_calls == []

    tool_msg = next(m for m in messages if isinstance(m, ToolMessage))
    assert tool_msg.tool_call_id == "c1"
    assert "The arguments of Read were not valid JSON: {broken" in str(tool_msg.content)
