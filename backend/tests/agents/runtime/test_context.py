from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    convert_to_openai_messages,
)

from mlops_agents.agents.catalog import AgentDefinition, Limits
from mlops_agents.agents.runtime.contract import (
    ApprovalPart,
    MessageStatus,
    MessageView,
    Part,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from mlops_agents.agents.tools.base import Preview
from mlops_agents.workspace.models import Project
from tests.agents.runtime.graph_env import prompt_messages


def _make_definition() -> AgentDefinition:
    return AgentDefinition(
        name="orchestrator",
        display_name="Orchestrator",
        description="The project assistant",
        tools=("Read", "Glob", "Grep"),
        chat=True,
        schedules=False,
        can_message=(),
        context_hooks=(),
        limits=Limits(max_steps=25),
        prompt="You are the orchestrator assistant.",
    )


def _make_project() -> Project:
    return Project(
        id="prj_123",
        name="mlops-agents",
        source="local",
        local_path="/path/to/repo",
        github_full_name=None,
        default_branch="main",
        status="ok",
        status_detail=None,
        created_at="2026-10-21T18:00:00Z",
        last_opened_at=None,
    )


def test_prompt_messages_system_message() -> None:
    definition = _make_definition()
    project = _make_project()
    layout = "src/\nREADME.md"
    summary = "Previous context summary"

    messages = prompt_messages(definition, project, layout, summary, [])
    assert len(messages) == 1
    system_msg = messages[0]
    assert isinstance(system_msg, SystemMessage)
    content = str(system_msg.content)

    assert "You are the orchestrator assistant." in content
    assert "mlops-agents" in content
    assert "local" in content
    assert "main" in content
    assert "src/\nREADME.md" in content
    assert "Summary of the earlier conversation:" in content
    assert "Previous context summary" in content


def test_prompt_messages_system_message_without_summary() -> None:
    definition = _make_definition()
    project = _make_project()
    layout = "README.md"

    messages = prompt_messages(definition, project, layout, None, [])
    assert len(messages) == 1
    assert "Summary of the earlier conversation:" not in str(messages[0].content)


def test_prompt_messages_system_message_without_default_branch() -> None:
    definition = _make_definition()
    project = _make_project()
    project_no_branch = project.model_copy(update={"default_branch": None})

    messages = prompt_messages(definition, project_no_branch, "README.md", None, [])
    assert len(messages) == 1
    assert "Default branch:" not in str(messages[0].content)


def test_prompt_messages_user_and_agent_turn() -> None:
    definition = _make_definition()
    project = _make_project()
    history = [
        MessageView(
            id="msg_u1",
            turn_id=None,
            author_kind="user",
            author_name="user",
            from_agent=None,
            parts=[TextPart(type="text", text="Check the project layout")],
            status="complete",
            created_at="2026-10-21T18:01:00Z",
        ),
        MessageView(
            id="msg_a1",
            turn_id="trn_1",
            author_kind="agent",
            author_name="orchestrator",
            from_agent=None,
            parts=[
                TextPart(type="text", text="I will check files."),
                ToolCallPart(
                    type="tool_call", id="call_1", name="Read", input={"path": "README.md"}
                ),
                ToolCallPart(type="tool_call", id="call_2", name="Glob", input={"pattern": "*.py"}),
                ToolResultPart(
                    type="tool_result", tool_call_id="call_1", content="# Title", is_error=False
                ),
                ToolResultPart(
                    type="tool_result", tool_call_id="call_2", content="main.py", is_error=False
                ),
            ],
            status="complete",
            created_at="2026-10-21T18:02:00Z",
        ),
    ]

    messages = prompt_messages(definition, project, "README.md", None, history)
    assert len(messages) == 5  # System, Human, AI, Tool1, Tool2
    assert isinstance(messages[1], HumanMessage)
    assert messages[1].content == "Check the project layout"

    ai_msg = messages[2]
    assert isinstance(ai_msg, AIMessage)
    assert ai_msg.content == "I will check files."
    assert len(ai_msg.tool_calls) == 2
    assert ai_msg.tool_calls[0]["id"] == "call_1"
    assert ai_msg.tool_calls[0]["name"] == "Read"
    assert ai_msg.tool_calls[0]["args"] == {"path": "README.md"}
    assert ai_msg.tool_calls[1]["id"] == "call_2"
    assert ai_msg.tool_calls[1]["name"] == "Glob"
    assert ai_msg.tool_calls[1]["args"] == {"pattern": "*.py"}

    tool_1 = messages[3]
    assert isinstance(tool_1, ToolMessage)
    assert tool_1.tool_call_id == "call_1"
    assert tool_1.content == "# Title"

    tool_2 = messages[4]
    assert isinstance(tool_2, ToolMessage)
    assert tool_2.tool_call_id == "call_2"
    assert tool_2.content == "main.py"

    # OpenAI validation
    openai_msgs: list[dict[str, Any]] = convert_to_openai_messages(messages[1:])
    tool_call_ids = [tc["id"] for m in openai_msgs if m.get("tool_calls") for tc in m["tool_calls"]]
    tool_resp_ids = [m["tool_call_id"] for m in openai_msgs if m.get("role") == "tool"]
    assert tool_call_ids == tool_resp_ids == ["call_1", "call_2"]


def test_prompt_messages_synthetic_tool_message_for_stopped_and_interrupted() -> None:
    definition = _make_definition()
    project = _make_project()

    history = [
        MessageView(
            id="msg_a_stopped",
            turn_id="trn_stopped",
            author_kind="agent",
            author_name="orchestrator",
            from_agent=None,
            parts=[
                TextPart(type="text", text="Calling tool then stopped"),
                ToolCallPart(
                    type="tool_call", id="call_stopped", name="Read", input={"path": "large.bin"}
                ),
            ],
            status="stopped",
            created_at="2026-10-21T18:03:00Z",
        ),
        MessageView(
            id="msg_a_interrupted",
            turn_id="trn_interrupted",
            author_kind="agent",
            author_name="orchestrator",
            from_agent=None,
            parts=[
                TextPart(type="text", text="Calling tool then interrupted"),
                ToolCallPart(
                    type="tool_call",
                    id="call_interrupted",
                    name="Read",
                    input={"path": "other.bin"},
                ),
            ],
            status="interrupted",
            created_at="2026-10-21T18:04:00Z",
        ),
    ]

    messages = prompt_messages(definition, project, "", None, history)
    # Expected: SystemMessage,
    # AIMessage(call_stopped), ToolMessage(call_stopped, "cancelled by the user"),
    # AIMessage(call_interrupted), ToolMessage(call_interrupted, "interrupted")
    assert len(messages) == 5

    assert isinstance(messages[2], ToolMessage)
    assert messages[2].tool_call_id == "call_stopped"
    assert messages[2].content == "cancelled by the user"

    assert isinstance(messages[4], ToolMessage)
    assert messages[4].tool_call_id == "call_interrupted"
    assert messages[4].content == "interrupted"

    openai_msgs = convert_to_openai_messages(messages[1:])
    tool_call_ids = [tc["id"] for m in openai_msgs if m.get("tool_calls") for tc in m["tool_calls"]]
    tool_resp_ids = [m["tool_call_id"] for m in openai_msgs if m.get("role") == "tool"]
    assert tool_call_ids == tool_resp_ids == ["call_stopped", "call_interrupted"]


def test_prompt_messages_omits_queued_and_system_messages() -> None:
    definition = _make_definition()
    project = _make_project()

    history = [
        MessageView(
            id="msg_queued",
            turn_id=None,
            author_kind="user",
            author_name="user",
            from_agent=None,
            parts=[TextPart(type="text", text="Queued message")],
            status="queued",
            created_at="2026-10-21T18:05:00Z",
        ),
        MessageView(
            id="msg_system",
            turn_id="trn_err",
            author_kind="system",
            author_name="system",
            from_agent=None,
            parts=[TextPart(type="text", text="Turn failed with an error")],
            status="complete",
            created_at="2026-10-21T18:06:00Z",
        ),
        MessageView(
            id="msg_valid",
            turn_id=None,
            author_kind="user",
            author_name="user",
            from_agent=None,
            parts=[TextPart(type="text", text="Valid user message")],
            status="complete",
            created_at="2026-10-21T18:07:00Z",
        ),
    ]

    messages = prompt_messages(definition, project, "", None, history)
    assert len(messages) == 2  # SystemMessage, HumanMessage("Valid user message")
    assert messages[1].content == "Valid user message"


def test_prompt_messages_from_agent_prefix() -> None:
    definition = _make_definition()
    project = _make_project()

    history = [
        MessageView(
            id="msg_agent_msg",
            turn_id=None,
            author_kind="user",
            author_name="coder",
            from_agent="coder",
            parts=[TextPart(type="text", text="Please inspect tests")],
            status="complete",
            created_at="2026-10-21T18:08:00Z",
        ),
    ]

    messages = prompt_messages(definition, project, "", None, history)
    assert len(messages) == 2
    assert isinstance(messages[1], HumanMessage)
    assert messages[1].content == "Message from coder:\nPlease inspect tests"


def _agent_message(parts: list[Part], status: MessageStatus) -> MessageView:
    return MessageView(
        id="msg_a",
        turn_id="trn_1",
        author_kind="agent",
        author_name="orchestrator",
        from_agent=None,
        parts=parts,
        status=status,
        created_at="2026-10-24T09:00:00Z",
    )


STALL_PART = ApprovalPart(
    type="approval",
    approval_id="apr_stall",
    tool_call_id=None,
    preview=Preview(kind="text", title="Stalled: no usable key", body="key 1: limited"),
    status="approved",
    note=None,
    kind="stall",
    step=1,
)
TOOL_APPROVAL_PART = ApprovalPart(
    type="approval",
    approval_id="apr_1",
    tool_call_id="c1",
    preview=Preview(kind="diff", title="Write a.py", body="+x"),
    status="approved",
    note=None,
)


@pytest.mark.parametrize("approval", [TOOL_APPROVAL_PART, STALL_PART], ids=["tool", "stall"])
def test_prompt_messages_skips_approval_parts(approval: ApprovalPart) -> None:
    call = ToolCallPart(type="tool_call", id="c1", name="Write", input={"path": "a.py"})
    result = ToolResultPart(type="tool_result", tool_call_id="c1", content="done", is_error=False)
    text = TextPart(type="text", text="Writing.")
    with_approval = prompt_messages(
        _make_definition(),
        _make_project(),
        "",
        None,
        [_agent_message([text, call, approval, result], "complete")],
    )
    without = prompt_messages(
        _make_definition(),
        _make_project(),
        "",
        None,
        [_agent_message([text, call, result], "complete")],
    )
    assert with_approval == without
    assert [type(m) for m in with_approval[1:]] == [AIMessage, ToolMessage]


def test_prompt_messages_appends_the_hook_context_as_the_last_system_section() -> None:
    definition = _make_definition()
    project = _make_project()
    layout = "README.md"
    hook_context = (
        '<data source="hook:status_board">\nStatus board:\n- Orchestrator: running\n</data>'
    )

    messages = prompt_messages(
        definition, project, layout, "Earlier things.", [], hook_context=hook_context
    )
    content = str(messages[0].content)
    assert content.endswith("\n\n" + hook_context)
    assert content.index("Summary of the earlier conversation:") < content.index(hook_context)

    # Without hook context
    messages_without = prompt_messages(definition, project, layout, None, [])
    assert '<data source="hook:' not in str(messages_without[0].content)


def test_prompt_messages_trims_older_tool_results_and_keeps_stored_intact() -> None:
    definition = _make_definition()
    project = _make_project()

    large_content = "x" * 2000
    stored_part = ToolResultPart(
        type="tool_result", tool_call_id="call_old", content=large_content, is_error=False
    )
    history = [
        MessageView(
            id="msg_old",
            turn_id="trn_old",
            author_kind="agent",
            author_name="orchestrator",
            from_agent=None,
            parts=[
                ToolCallPart(
                    type="tool_call", id="call_old", name="Read", input={"file_path": "a.txt"}
                ),
                stored_part,
            ],
            status="complete",
            created_at="2026-10-21T18:00:00Z",
        )
    ]

    messages = prompt_messages(definition, project, "", None, history)
    # messages: [SystemMessage, AIMessage, ToolMessage]
    assert len(messages) == 3
    tool_msg = messages[2]
    assert isinstance(tool_msg, ToolMessage)
    assert tool_msg.tool_call_id == "call_old"
    # Content must be trimmed to head (500) + notice
    assert len(str(tool_msg.content)) < 2000
    assert str(tool_msg.content).startswith("x" * 500)
    assert "earlier output truncated" in str(tool_msg.content)
    assert "Full output is in the session." in str(tool_msg.content)

    # Stored part in message view remains untouched
    assert stored_part.content == large_content
