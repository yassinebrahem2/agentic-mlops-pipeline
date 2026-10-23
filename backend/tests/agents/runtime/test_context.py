from pathlib import Path
from typing import Any

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    convert_to_openai_messages,
)

from mlops_agents.agents.catalog import AgentDefinition, Limits
from mlops_agents.agents.runtime.context import build_messages, top_level_layout
from mlops_agents.agents.runtime.contract import (
    MessageView,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from mlops_agents.workspace.models import Project


class FakeFiles:
    def __init__(self, tracked_paths: list[str]) -> None:
        self._paths = tracked_paths

    @property
    def root(self) -> Path:
        return Path("/repo")

    async def tracked(self) -> list[str]:
        return list(self._paths)

    async def under(self, prefix: str) -> list[str]:
        return [p for p in self._paths if p.startswith(prefix)]

    async def resolve_file(self, user_path: str) -> str:
        return user_path

    async def resolve_dir(self, user_path: str | None) -> str:
        return user_path or ""


async def test_top_level_layout_grouping_and_sorting() -> None:
    files = FakeFiles(
        [
            "src/mlops_agents/main.py",
            "src/mlops_agents/utils.py",
            "README.md",
            "tests/test_store.py",
            "pyproject.toml",
            ".gitignore",
        ]
    )
    layout = await top_level_layout(files)
    expected = "\n".join([".gitignore", "README.md", "pyproject.toml", "src/", "tests/"])
    assert layout == expected


async def test_top_level_layout_caps_at_100() -> None:
    # 105 files in root
    paths = [f"file_{i:03d}.txt" for i in range(105)]
    files = FakeFiles(paths)
    layout = await top_level_layout(files)
    lines = layout.splitlines()
    assert len(lines) == 101
    assert lines[0] == "file_000.txt"
    assert lines[99] == "file_099.txt"
    assert lines[100] == "(and 5 more)"


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


def test_build_messages_system_message() -> None:
    definition = _make_definition()
    project = _make_project()
    layout = "src/\nREADME.md"
    summary = "Previous context summary"

    messages = build_messages(definition, project, layout, summary, [])
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


def test_build_messages_system_message_without_summary() -> None:
    definition = _make_definition()
    project = _make_project()
    layout = "README.md"

    messages = build_messages(definition, project, layout, None, [])
    assert len(messages) == 1
    assert "Summary of the earlier conversation:" not in str(messages[0].content)


def test_build_messages_system_message_without_default_branch() -> None:
    definition = _make_definition()
    project = _make_project()
    project_no_branch = project.model_copy(update={"default_branch": None})

    messages = build_messages(definition, project_no_branch, "README.md", None, [])
    assert len(messages) == 1
    assert "Default branch:" not in str(messages[0].content)


def test_build_messages_user_and_agent_turn() -> None:
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

    messages = build_messages(definition, project, "README.md", None, history)
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


def test_build_messages_synthetic_tool_message_for_stopped_and_interrupted() -> None:
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

    messages = build_messages(definition, project, "", None, history)
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


def test_build_messages_omits_queued_and_system_messages() -> None:
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

    messages = build_messages(definition, project, "", None, history)
    assert len(messages) == 2  # SystemMessage, HumanMessage("Valid user message")
    assert messages[1].content == "Valid user message"


def test_build_messages_from_agent_prefix() -> None:
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

    messages = build_messages(definition, project, "", None, history)
    assert len(messages) == 2
    assert isinstance(messages[1], HumanMessage)
    assert messages[1].content == "Message from coder:\nPlease inspect tests"
