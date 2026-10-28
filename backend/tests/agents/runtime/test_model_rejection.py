from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx2
from langchain_core.messages import BaseMessage, HumanMessage

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.agent import AgentBuilder
from mlops_agents.agents.runtime.contract import TextPart
from mlops_agents.agents.runtime.executor import TurnExecutor
from mlops_agents.agents.runtime.hub import AsyncRLock
from mlops_agents.agents.runtime.model_caller import ModelCaller
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools import default_registry
from mlops_agents.config import RunMode, load_settings
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.providers.failover import KeyFailover
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.workspace.service import WorkspaceService
from tests.agents.runtime.graph_env import RecordingSink, stage1_hooks
from tests.conftest import STAGE_2_TOOLS, MemoryKeyStore, make_repo, memory_project_secrets
from tests.fakes.fake_openai import RunningFake
from tests.fakes.workspaces import FakeWorkspaces
from tests.integrations.test_github_auth import FakeGh
from tests.providers.fake_clock import FakeClock

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "Read",
            "description": "read a file",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        },
    }
]


@dataclass
class _RejectionEnv:
    executor: TurnExecutor
    store: SessionStore
    caller: ModelCaller
    providers: ProvidersService
    resolved_model: ResolvedModel
    project_id: str
    session_id: str
    turn_id: str


async def _setup_rejection_env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    *,
    wire_model_id: str = "fake-tools",
) -> _RejectionEnv:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "Line 1 of readme\nLine 2\n"}, git_env)
    settings = replace(
        load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)}),
        mode=RunMode.LOCAL,
    )
    tools = default_registry()
    catalog = AgentCatalog.load(tools.names() | STAGE_2_TOOLS)
    failover = KeyFailover(FakeClock())
    providers = ProvidersService(databases, keystore, model_http, failover)
    agent_settings = AgentSettingsService(databases, providers, catalog)
    github = GitHubAuth(databases, keystore, httpx2.AsyncClient(), FakeGh(None))
    workspace = WorkspaceService(
        settings,
        paths,
        databases,
        GitRunner(paths.bin, github.token),
        github,
        httpx2.AsyncClient(),
        memory_project_secrets(databases),
    )
    workspaces = FakeWorkspaces(workspace=workspace, git=GitRunner(paths.bin))
    builder = AgentBuilder(
        catalog=catalog,
        tools=tools,
        settings=agent_settings,
        workspace=workspace,
        workspaces=workspaces,
    )
    store = SessionStore(databases)
    caller = ModelCaller(providers, model_http, failover)
    executor = TurnExecutor(
        store=store, caller=caller, builder=builder, hooks=stage1_hooks(workspaces, databases)
    )

    prov = await providers.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake.base_url)
    )
    await providers.add_model(ModelCreate(provider=prov.name, wire_model_id=wire_model_id))
    entry = await providers.verify_model(f"{prov.name}:{wire_model_id}")
    stored_keys = tuple(await providers.keys_for(prov.id))
    assert entry.context_window is not None
    resolved = ResolvedModel(
        address=entry.address,
        provider_id=prov.id,
        provider_name=prov.name,
        base_url=prov.base_url,
        wire_model_id=entry.wire_model_id,
        context_window=entry.context_window,
        keys=stored_keys,
    )

    project = await workspace.add_local(str(repo_dir))
    await agent_settings.set_default_model("orchestrator", f"{prov.name}:{wire_model_id}")
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

    fake.state.reset()
    return _RejectionEnv(
        executor=executor,
        store=store,
        caller=caller,
        providers=providers,
        resolved_model=resolved,
        project_id=project.id,
        session_id=session_id,
        turn_id=turn_id,
    )


async def test_first_request_rejected_retries_with_corrective_message_and_completes_done(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env = await _setup_rejection_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    fake_openai.state.bad_generation_remaining = 1
    fake_openai.state.bad_generation_code = "tool_use_failed"
    fake_openai.state.bad_generation_message = "Tool call validation failed"

    sink = RecordingSink()
    end_status, error_text = await env.executor.run(
        env.project_id, env.session_id, "orchestrator", env.turn_id, sink, AsyncRLock()
    )

    assert end_status == "done"
    assert error_text is None

    # Check requests to provider: exactly 3 requests in total
    # Request 1: initial prompt -> rejected (400)
    # Request 2: retry with corrective message -> succeeds with tool call
    # Request 3: tool result sent -> succeeds with final text
    assert len(fake_openai.state.requests) == 3
    req2 = fake_openai.state.requests[1]
    messages_in_req2 = req2.get("messages", [])
    expected_substr = (
        "Your last tool call was rejected by the provider: Tool call validation failed. "
        "Call the tool again with every required argument, or answer without it."
    )
    assert any(expected_substr in str(m.get("content")) for m in messages_in_req2)

    # Verify nothing about the rejection is stored in messages
    stored_msgs = await env.store.messages(env.project_id, env.session_id)
    for msg in stored_msgs:
        for part in msg.parts:
            if part.type == "text":
                assert "rejected by the provider" not in part.text


async def test_retry_preserves_only_retried_attempt_tool_calls(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env = await _setup_rejection_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    fake_openai.state.bad_generation_remaining = 1
    fake_openai.state.bad_generation_code = "tool_use_failed"

    original_message = HumanMessage(content="Check readme")
    input_messages: list[BaseMessage] = [original_message]

    async def _noop(_: str) -> None:
        pass

    reply = await env.caller.call(
        env.resolved_model,
        input_messages,
        tools=TOOLS,
        on_text=_noop,
    )

    # Input messages must NOT be modified
    assert len(input_messages) == 1
    assert input_messages[0].content == original_message.content

    # Reply holds tool calls from the successful retry
    assert len(reply.message.tool_calls) == 1
    assert reply.message.tool_calls[0]["name"] == "Read"
    assert reply.message.tool_calls[0]["args"] == {"file_path": "README.md"}


async def test_three_rejections_fails_turn_after_three_requests(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env = await _setup_rejection_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    fake_openai.state.bad_generation_remaining = 3
    fake_openai.state.bad_generation_code = "tool_use_failed"
    fake_openai.state.bad_generation_message = "Tool call validation failed repeatedly"

    sink = RecordingSink()
    end_status, error_text = await env.executor.run(
        env.project_id, env.session_id, "orchestrator", env.turn_id, sink, AsyncRLock()
    )

    assert end_status == "failed"
    assert error_text is not None
    assert "Tool call validation failed repeatedly" in error_text
    assert len(fake_openai.state.requests) == 3


async def test_sse_error_event_after_role_chunk_retried_and_completes_done(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env = await _setup_rejection_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    fake_openai.state.sse_error_remaining = 1
    fake_openai.state.sse_error_code = "tool_use_failed"
    fake_openai.state.sse_error_message = "Tool call validation failed in SSE"

    sink = RecordingSink()
    end_status, error_text = await env.executor.run(
        env.project_id, env.session_id, "orchestrator", env.turn_id, sink, AsyncRLock()
    )

    assert end_status == "done"
    assert error_text is None
    # 3 requests: request 1 (SSE error) -> request 2 (retry with tool call) -> request 3 (tool result -> text)
    assert len(fake_openai.state.requests) == 3
    req2 = fake_openai.state.requests[1]
    messages_in_req2 = req2.get("messages", [])
    expected_substr = (
        "Your last tool call was rejected by the provider: Tool call validation failed in SSE. "
        "Call the tool again with every required argument, or answer without it."
    )
    assert any(expected_substr in str(m.get("content")) for m in messages_in_req2)


async def test_different_400_fails_on_first_request(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env = await _setup_rejection_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    fake_openai.state.bad_generation_remaining = 1
    fake_openai.state.bad_generation_code = "invalid_parameter"
    fake_openai.state.bad_generation_message = "Invalid model parameter"

    sink = RecordingSink()
    end_status, error_text = await env.executor.run(
        env.project_id, env.session_id, "orchestrator", env.turn_id, sink, AsyncRLock()
    )

    assert end_status == "failed"
    assert error_text is not None
    assert "Invalid model parameter" in error_text
    assert len(fake_openai.state.requests) == 1
