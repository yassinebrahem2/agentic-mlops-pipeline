from __future__ import annotations

from typing import cast

import pytest

from mlops_agents.agents.catalog import AgentCatalog, AgentDefinition
from mlops_agents.agents.runtime.contract import TaskView
from mlops_agents.agents.tools import ListAgentsTool, default_registry
from mlops_agents.agents.tools.base import FileAccess, RiskClass, ToolContext
from mlops_agents.events.contract import AgentStatus, PrRef


def _make_def(
    name: str,
    display_name: str,
    description: str,
    chat: bool = True,
    can_message: tuple[str, ...] = (),
) -> AgentDefinition:
    return AgentDefinition(
        name=name,
        display_name=display_name,
        description=description,
        chat=chat,
        can_message=can_message,
        prompt="Test prompt",
    )


class FakeStatusBoard:
    def __init__(self, statuses: list[AgentStatus], catalog: AgentCatalog | None = None) -> None:
        self._statuses = statuses
        if catalog is not None:
            self._catalog = catalog

    async def snapshot(self, project_id: str) -> list[AgentStatus]:
        return self._statuses

    async def summary(self, project_id: str) -> str:
        return ""


def _ctx(agent: str = "orchestrator", project_id: str = "prj_test") -> ToolContext:
    return ToolContext(
        project_id=project_id,
        agent=agent,
        session_id="ses_1",
        turn_id="trn_1",
        files=cast(FileAccess, object()),
        task=None,
    )


@pytest.fixture
def sample_catalog() -> AgentCatalog:
    d_orch = _make_def(
        name="orchestrator",
        display_name="Orchestrator",
        description="Coordinates tasks and plans.",
        chat=True,
        can_message=("coder",),
    )
    d_coder = _make_def(
        name="coder",
        display_name="Coder",
        description="Implements features and writes tests.",
        chat=True,
        can_message=("reviewer",),
    )
    d_rev = _make_def(
        name="reviewer",
        display_name="Reviewer",
        description="Reviews pull requests and provides feedback.",
        chat=True,
        can_message=(),
    )
    d_inactive = _make_def(
        name="metrics-collector",
        display_name="Metrics Collector",
        description="Collects background metrics.",
        chat=False,
        can_message=(),
    )
    return AgentCatalog([d_orch, d_coder, d_rev, d_inactive], tool_names=frozenset())


@pytest.fixture
def sample_board() -> FakeStatusBoard:
    task_view = TaskView(
        id="tsk_123",
        agent="coder",
        title="Build feature X",
        branch="plan/feature-x",
        status="active",
        review_kind=None,
        pr_number=None,
        pr_url=None,
    )
    prs = [
        PrRef(number=12, url="https://github.com/org/repo/pull/12", title="Add feature X"),
        PrRef(number=15, url="https://github.com/org/repo/pull/15", title="Fix bug Y"),
    ]
    statuses = [
        AgentStatus(
            agent="orchestrator",
            display_name="Orchestrator",
            state="idle",
            task=None,
            open_prs=[],
            last_finished="Done.",
        ),
        AgentStatus(
            agent="coder",
            display_name="Coder",
            state="running",
            task=task_view,
            open_prs=prs,
            last_finished=None,
        ),
        AgentStatus(
            agent="reviewer",
            display_name="Reviewer",
            state="waiting_approval",
            task=None,
            open_prs=[],
            last_finished=None,
        ),
        AgentStatus(
            agent="metrics-collector",
            display_name="Metrics Collector",
            state="inactive",
            task=None,
            open_prs=[],
            last_finished=None,
        ),
    ]
    return FakeStatusBoard(statuses)


async def test_plain_text_output_for_crafted_status_board(
    sample_catalog: AgentCatalog, sample_board: FakeStatusBoard
) -> None:
    tool = ListAgentsTool(catalog=sample_catalog, status_board=sample_board)
    out = await tool.invoke(_ctx(agent="orchestrator"), {})

    assert not out.is_error
    blocks = out.content.split("\n\n")
    assert len(blocks) == 4

    expected_blocks = [
        "\n".join(
            [
                "Name: orchestrator",
                "Display name: Orchestrator",
                "Description: Coordinates tasks and plans.",
                "Chats: yes",
                "State: idle",
                "Current task: none",
                "Open PRs: none",
                "May message: no",
            ]
        ),
        "\n".join(
            [
                "Name: coder",
                "Display name: Coder",
                "Description: Implements features and writes tests.",
                "Chats: yes",
                "State: running",
                "Current task: Build feature X",
                "Open PRs: #12: Add feature X, #15: Fix bug Y",
                "May message: yes",
            ]
        ),
        "\n".join(
            [
                "Name: reviewer",
                "Display name: Reviewer",
                "Description: Reviews pull requests and provides feedback.",
                "Chats: yes",
                "State: waiting_approval",
                "Current task: none",
                "Open PRs: none",
                "May message: no",
            ]
        ),
        "\n".join(
            [
                "Name: metrics-collector",
                "Display name: Metrics Collector",
                "Description: Collects background metrics.",
                "Chats: no",
                "State: inactive",
                "Current task: none",
                "Open PRs: none",
                "May message: no",
            ]
        ),
    ]
    assert blocks == expected_blocks
    assert tool.risk is RiskClass.READ
    assert tool.name == "ListAgents"


async def test_may_message_follows_can_message(
    sample_catalog: AgentCatalog, sample_board: FakeStatusBoard
) -> None:
    tool = ListAgentsTool(catalog=sample_catalog, status_board=sample_board)

    # Coder can message reviewer only
    out = await tool.invoke(_ctx(agent="coder"), {})
    assert not out.is_error
    blocks = out.content.split("\n\n")

    # Map name -> may message
    may_msg: dict[str, str] = {}
    for block in blocks:
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        may_msg[lines["Name"]] = lines["May message"]

    assert may_msg["orchestrator"] == "no"
    assert may_msg["coder"] == "no"
    assert may_msg["reviewer"] == "yes"
    assert may_msg["metrics-collector"] == "no"


async def test_unknown_caller_or_caller_without_can_message(
    sample_catalog: AgentCatalog, sample_board: FakeStatusBoard
) -> None:
    tool = ListAgentsTool(catalog=sample_catalog, status_board=sample_board)

    # Reviewer has empty can_message
    out_rev = await tool.invoke(_ctx(agent="reviewer"), {})
    for block in out_rev.content.split("\n\n"):
        assert "May message: no" in block

    # Unknown caller
    out_unk = await tool.invoke(_ctx(agent="stranger"), {})
    for block in out_unk.content.split("\n\n"):
        assert "May message: no" in block


async def test_invalid_arguments_returns_error(
    sample_catalog: AgentCatalog, sample_board: FakeStatusBoard
) -> None:
    tool = ListAgentsTool(catalog=sample_catalog, status_board=sample_board)
    out = await tool.invoke(_ctx(), {"extra_param": "not_allowed"})
    assert out.is_error
    assert "Invalid arguments for ListAgents" in out.content


def test_default_registry_integration(
    sample_catalog: AgentCatalog, sample_board: FakeStatusBoard
) -> None:
    # Explicit status_board and catalog
    reg = default_registry(status_board=sample_board, catalog=sample_catalog)
    assert "ListAgents" in reg.names()
    assert isinstance(reg.get("ListAgents"), ListAgentsTool)

    # Status board with _catalog attribute
    board_with_catalog = FakeStatusBoard([], catalog=sample_catalog)
    reg2 = default_registry(status_board=board_with_catalog)
    assert "ListAgents" in reg2.names()

    # Without status_board
    reg_empty = default_registry()
    assert "ListAgents" not in reg_empty.names()


async def test_open_pr_without_title_formatting(sample_catalog: AgentCatalog) -> None:
    board = FakeStatusBoard(
        [
            AgentStatus(
                agent="coder",
                display_name="Coder",
                state="running",
                task=None,
                open_prs=[PrRef(number=42, url="https://example.com/pr/42", title="")],
                last_finished=None,
            )
        ]
    )
    tool = ListAgentsTool(catalog=sample_catalog, status_board=board)
    out = await tool.invoke(_ctx(agent="coder"), {})
    assert not out.is_error
    assert "Open PRs: #42\n" in out.content


async def test_agent_in_catalog_missing_from_snapshot(sample_catalog: AgentCatalog) -> None:
    # Status board snapshot is completely empty
    empty_board = FakeStatusBoard([])
    tool = ListAgentsTool(catalog=sample_catalog, status_board=empty_board)
    out = await tool.invoke(_ctx(agent="orchestrator"), {})
    assert not out.is_error
    blocks = out.content.split("\n\n")
    # All 4 catalog agents should still be listed
    assert len(blocks) == 4
    assert "Name: orchestrator" in blocks[0]
    assert "State: idle" in blocks[0]
    assert "Current task: none" in blocks[0]
    assert "Open PRs: none" in blocks[0]

    # Metrics collector (chat=False) should have State: inactive
    inactive_block = next(b for b in blocks if "Name: metrics-collector" in b)
    assert "Chats: no" in inactive_block
    assert "State: inactive" in inactive_block


def test_tool_schema(sample_catalog: AgentCatalog, sample_board: FakeStatusBoard) -> None:
    tool = ListAgentsTool(catalog=sample_catalog, status_board=sample_board)
    schema = tool.schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "ListAgents"
    assert "description" in schema["function"]
    assert "parameters" in schema["function"]
