from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from mlops_agents.agents.runtime.context import history_messages
from mlops_agents.agents.runtime.contract import MessageView, TextPart, ToolCallPart, ToolResultPart
from mlops_agents.agents.runtime.graph import TurnState, create_turn_graph
from mlops_agents.agents.runtime.hub import AsyncRLock
from mlops_agents.agents.runtime.model_caller import ModelCaller, ModelReply
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.paths import DataPaths
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.graph_env import RecordingSink, graph_hooks, make_agent
from tests.conftest import make_repo


def _make_tool_call_part(
    call_id: str,
    name: str = "Read",
    args: dict[str, Any] | None = None,
    extra_content: dict[str, Any] | None = None,
    model: str | None = None,
) -> ToolCallPart:
    return ToolCallPart(
        type="tool_call",
        id=call_id,
        name=name,
        input=args or {"file_path": "README.md"},
        extra_content=extra_content,
        model=model,
    )


def test_tool_call_part_fields() -> None:
    part_without = ToolCallPart(
        type="tool_call",
        id="call_1",
        name="Read",
        input={"path": "a"},
    )
    assert part_without.extra_content is None
    assert part_without.model is None

    part_with = ToolCallPart(
        type="tool_call",
        id="call_1",
        name="Read",
        input={"path": "a"},
        extra_content={"google": {"thought_signature": "sig"}},
        model="openrouter/google/gemini-3",
    )
    assert part_with.extra_content == {"google": {"thought_signature": "sig"}}
    assert part_with.model == "openrouter/google/gemini-3"


def test_history_messages_preserves_extra_content_matching_model() -> None:
    history = [
        MessageView(
            id="msg_1",
            turn_id="turn_1",
            author_kind="agent",
            author_name="agent",
            from_agent=None,
            parts=[
                _make_tool_call_part(
                    "call_1",
                    extra_content={"google": {"thought_signature": "sig_1"}},
                    model="google/gemini-3",
                ),
                ToolResultPart(
                    type="tool_result",
                    tool_call_id="call_1",
                    content="output 1",
                    is_error=False,
                ),
            ],
            status="complete",
            created_at="2026-10-31T00:00:00Z",
        )
    ]

    # Matching model
    messages = history_messages(history, model="google/gemini-3")
    assert len(messages) >= 1
    ai_msg = next(m for m in messages if isinstance(m, AIMessage))
    assert ai_msg.additional_kwargs.get("tool_calls_extra_content", {}).get("call_1") == {
        "google": {"thought_signature": "sig_1"}
    }
    assert ai_msg.additional_kwargs.get("tool_calls_model", {}).get("call_1") == "google/gemini-3"


def test_history_messages_drops_extra_content_mismatched_model() -> None:
    history = [
        MessageView(
            id="msg_1",
            turn_id="turn_1",
            author_kind="agent",
            author_name="agent",
            from_agent=None,
            parts=[
                _make_tool_call_part(
                    "call_1",
                    extra_content={"google": {"thought_signature": "sig_1"}},
                    model="google/gemini-3",
                ),
                ToolResultPart(
                    type="tool_result",
                    tool_call_id="call_1",
                    content="output 1",
                    is_error=False,
                ),
            ],
            status="complete",
            created_at="2026-10-31T00:00:00Z",
        )
    ]

    # Mismatched model address
    messages = history_messages(history, model="openai/gpt-4o")
    ai_msg = next(m for m in messages if isinstance(m, AIMessage))
    assert (
        "tool_calls_extra_content" not in ai_msg.additional_kwargs
        or not ai_msg.additional_kwargs["tool_calls_extra_content"]
    )


def test_history_messages_without_signatures_stays_empty() -> None:
    history = [
        MessageView(
            id="msg_1",
            turn_id="turn_1",
            author_kind="agent",
            author_name="agent",
            from_agent=None,
            parts=[
                _make_tool_call_part("call_1", extra_content=None, model="google/gemini-3"),
                ToolResultPart(
                    type="tool_result",
                    tool_call_id="call_1",
                    content="output 1",
                    is_error=False,
                ),
            ],
            status="complete",
            created_at="2026-10-31T00:00:00Z",
        )
    ]

    messages = history_messages(history, model="google/gemini-3")
    ai_msg = next(m for m in messages if isinstance(m, AIMessage))
    assert not ai_msg.additional_kwargs.get("tool_calls_extra_content")


class _MockCaller(ModelCaller):
    def __init__(self, replies: list[ModelReply]) -> None:
        self._replies = list(replies)
        self.call_count = 0

    async def call(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        model: ResolvedModel,
        messages: list[Any],
        tools: list[dict[str, Any]],
        on_text: Any,
        on_restart: Any = None,
    ) -> ModelReply:
        self.call_count += 1
        reply = self._replies.pop(0)
        if reply.message.content and isinstance(reply.message.content, str):
            res = on_text(reply.message.content)
            if asyncio.iscoroutine(res):
                await res
        return reply


async def test_graph_persists_tool_call_extra_content_and_restart_rebuilds_it(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    git_env: dict[str, str],
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"README.md": "Line 1\nLine 2"}, git_env)
    agent = make_agent(repo_dir, paths.bin, project_id="prj_1")
    store = SessionStore(databases)
    sink = RecordingSink()

    session_id = await store.session_id("prj_1", "orchestrator")
    user_msg = await store.add_message(
        "prj_1",
        session_id,
        author_kind="user",
        author_name="User",
        from_agent=None,
        parts=[TextPart(type="text", text="Read the file")],
        status="complete",
    )
    turn_id = await store.add_turn("prj_1", session_id, user_msg.id, "running")
    agent_msg = await store.add_message(
        "prj_1",
        session_id,
        author_kind="agent",
        author_name=agent.definition.name,
        from_agent=None,
        parts=[],
        status="streaming",
        turn_id=turn_id,
    )

    caller = _MockCaller(
        [
            ModelReply(
                message=AIMessage(
                    content="",
                    tool_calls=[
                        {"id": "call_1", "name": "Read", "args": {"file_path": "README.md"}}
                    ],
                    additional_kwargs={
                        "tool_calls_extra_content": {
                            "call_1": {"google": {"thought_signature": "sig_xyz"}}
                        }
                    },
                ),
                prompt_tokens=10,
                completion_tokens=5,
            ),
            ModelReply(
                message=AIMessage(content="Finished reading file"),
                prompt_tokens=15,
                completion_tokens=5,
            ),
        ]
    )

    lock = AsyncRLock()
    hooks = graph_hooks(agent, databases)
    current_parts: list[Any] = []
    graph = create_turn_graph(
        agent=agent,
        caller=caller,
        store=store,
        sink=sink,
        project_id="prj_1",
        session_id=session_id,
        turn_id=turn_id,
        message_id=agent_msg.id,
        current_parts=current_parts,
        lock=lock,
        hooks=hooks,
    )
    initial: TurnState = {
        "messages": [HumanMessage(content="Read the file")],
        "step_count": 0,
        "stopped_reason": None,
    }
    await graph.ainvoke(initial)  # pyright: ignore[reportUnknownMemberType]

    # Verify that stored message has ToolCallPart with extra_content and model
    stored_msgs = await store.messages("prj_1", session_id)
    agent_stored = next(m for m in stored_msgs if m.id == agent_msg.id)
    tc_part = next(p for p in agent_stored.parts if isinstance(p, ToolCallPart))
    assert tc_part.extra_content == {"google": {"thought_signature": "sig_xyz"}}
    assert tc_part.model == agent.model.address

    # Now verify later turn / restart rebuilds history with extra_content preserved
    rebuilt_messages = history_messages(stored_msgs, model=agent.model.address)
    rebuilt_ai = next(m for m in rebuilt_messages if isinstance(m, AIMessage) and m.tool_calls)
    assert rebuilt_ai.additional_kwargs.get("tool_calls_extra_content", {}).get("call_1") == {
        "google": {"thought_signature": "sig_xyz"}
    }
    assert (
        rebuilt_ai.additional_kwargs.get("tool_calls_model", {}).get("call_1")
        == agent.model.address
    )
