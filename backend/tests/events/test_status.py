"""Tests for StatusBoard and agent status derivation (Task 3 of Stage 2 Events)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mlops_agents.agents.catalog import AgentCatalog, AgentDefinition
from mlops_agents.agents.hooks.contract import HookContext
from mlops_agents.agents.runtime.contract import TaskView
from mlops_agents.events.contract import EventRecord, EventType
from mlops_agents.events.hub import EventHub
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.events.status import (
    SqliteStatusBoard,
    derive_agent_status,
    format_agent_status_line,
)
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.builder_env import status_board_hooks
from tests.api.fake_sessions import FakeSessions
from tests.fakes.fake_tasks import FakeTasks


def make_definition(
    name: str = "orchestrator",
    display_name: str = "Orchestrator",
    chat: bool = True,
    status_board: bool = False,
) -> AgentDefinition:
    return AgentDefinition(
        name=name,
        display_name=display_name,
        description=f"Description for {name}",
        tools=(),
        chat=chat,
        schedules=False,
        can_message=(),
        context_hooks=(),
        status_board=status_board,
        prompt="Test prompt",
    )


def make_event(
    seq: int,
    event_type: EventType,
    source: str = "orchestrator",
    subject_id: str | None = None,
    payload: dict[str, object] | None = None,
) -> EventRecord:
    return EventRecord(
        id=f"evt_{seq}",
        seq=seq,
        ts="2026-10-24T12:00:00Z",
        source=source,
        type=event_type,
        subject_id=subject_id,
        payload=payload or {},
    )


def test_derive_agent_status_inactive_when_chat_false() -> None:
    defn = make_definition(
        name="alert-prioritization", display_name="Alert Prioritization", chat=False
    )
    # Even if there are events and turn_state="running", chat=False is always inactive
    events = [
        make_event(1, "turn.started", source="alert-prioritization", payload={"turn_id": "trn_1"}),
    ]
    status = derive_agent_status(defn, events, turn_state="running")
    assert status.state == "inactive"
    assert status.agent == "alert-prioritization"
    assert status.display_name == "Alert Prioritization"


def test_derive_agent_status_running_from_turn_state() -> None:
    defn = make_definition()
    status = derive_agent_status(defn, [], turn_state="running")
    assert status.state == "running"
    assert status.last_finished is None


def test_derive_agent_status_waiting_approval_from_turn_state() -> None:
    defn = make_definition()
    status = derive_agent_status(defn, [], turn_state="waiting")
    assert status.state == "waiting_approval"


def test_derive_agent_status_idle_and_last_finished() -> None:
    defn = make_definition()
    final_text = "Result: " + "X" * 150
    events = [
        make_event(
            1,
            "turn.ended",
            source="orchestrator",
            payload={"turn_id": "trn_1", "status": "done", "final_text": final_text},
        ),
    ]
    status = derive_agent_status(defn, events, turn_state=None)
    assert status.state == "idle"
    assert status.last_finished == final_text


def test_derive_agent_status_waiting_reply_lifecycle() -> None:
    defn_a = make_definition(name="agent-a", display_name="Agent A")
    defn_b = make_definition(name="agent-b", display_name="Agent B")

    # 1. Agent A sends message in exchange xch_1 to Agent B (reading only fixed keys: to, exchange_id, hop)
    events = [
        make_event(
            1,
            "message.sent",
            source="agent-a",
            payload={"to": "agent-b", "exchange_id": "xch_1", "hop": 1},
        )
    ]
    status_a = derive_agent_status(defn_a, events)
    status_b = derive_agent_status(defn_b, events)
    assert status_a.state == "waiting_reply"
    assert status_b.state == "idle"

    # 2. Agent B replies to Agent A in exchange xch_1
    events.append(
        make_event(
            2,
            "message.sent",
            source="agent-b",
            payload={"to": "agent-a", "exchange_id": "xch_1", "hop": 2},
        )
    )
    status_a = derive_agent_status(defn_a, events)
    status_b = derive_agent_status(defn_b, events)
    assert status_a.state == "idle"
    assert status_b.state == "waiting_reply"

    # 3. Agent A sends in another exchange xch_2 to Agent C
    events.append(
        make_event(
            3,
            "message.sent",
            source="agent-a",
            payload={"to": "agent-c", "exchange_id": "xch_2", "hop": 1},
        )
    )
    status_a = derive_agent_status(defn_a, events)
    # The last exchange Agent A sent into is now xch_2, which has no reply yet
    assert status_a.state == "waiting_reply"


def test_derive_agent_status_open_prs_from_open_tasks() -> None:
    defn = make_definition(name="retraining", display_name="Retraining")
    events = [
        make_event(
            1,
            "pr.opened",
            source="retraining",
            payload={
                "number": 42,
                "url": "https://github.com/org/repo/pull/42",
                "title": "Retrain model v1",
                "task_id": "tsk_1",
            },
        ),
        make_event(
            2,
            "pr.updated",
            source="retraining",
            payload={
                "number": 42,
                "url": "https://github.com/org/repo/pull/42",
                "title": "Retrain model v2",
                "task_id": "tsk_1",
            },
        ),
    ]

    # Without open_tasks: no open PRs shown
    status_no_tasks = derive_agent_status(defn, events, open_tasks=None)
    assert len(status_no_tasks.open_prs) == 0

    # With open_tasks where tsk_1 is in_review
    task = TaskView(
        id="tsk_1",
        agent="retraining",
        title="Retrain model",
        branch="plan/retrain",
        status="in_review",
        review_kind="github",
        pr_number=42,
        pr_url="https://github.com/org/repo/pull/42",
    )
    status_with_tasks = derive_agent_status(defn, events, open_tasks=[task])
    assert len(status_with_tasks.open_prs) == 1
    assert status_with_tasks.open_prs[0].number == 42
    assert status_with_tasks.open_prs[0].title == "Retrain model v2"
    assert status_with_tasks.open_prs[0].url == "https://github.com/org/repo/pull/42"

    # When task is closed: no open PRs
    closed_task = TaskView(
        id="tsk_1",
        agent="retraining",
        title="Retrain model",
        branch="plan/retrain",
        status="closed",
        review_kind="github",
        pr_number=42,
        pr_url="https://github.com/org/repo/pull/42",
    )
    status_closed = derive_agent_status(defn, events, open_tasks=[closed_task])
    assert len(status_closed.open_prs) == 0


def test_format_agent_status_line() -> None:
    defn = make_definition()
    status_idle = derive_agent_status(defn, [])
    assert format_agent_status_line(status_idle) == "- Orchestrator: idle"

    # With task
    task = TaskView(
        id="tsk_123",
        agent="orchestrator",
        title="Implement events",
        branch="plan/events",
        status="active",
        review_kind=None,
        pr_number=None,
        pr_url=None,
    )
    status_with_task = derive_agent_status(defn, [], open_tasks=[task])
    assert (
        format_agent_status_line(status_with_task) == "- Orchestrator: idle, task: Implement events"
    )

    # With open PRs
    events = [
        make_event(
            1,
            "pr.opened",
            source="orchestrator",
            payload={
                "task_id": "tsk_123",
                "number": 10,
                "url": "",
                "title": "PR 10",
            },
        ),
        make_event(
            2,
            "pr.opened",
            source="orchestrator",
            payload={
                "task_id": "tsk_123",
                "number": 12,
                "url": "",
                "title": "PR 12",
            },
        ),
    ]
    task_in_review = TaskView(
        id="tsk_123",
        agent="orchestrator",
        title="Implement events",
        branch="plan/events",
        status="in_review",
        review_kind=None,
        pr_number=None,
        pr_url=None,
    )
    status_with_prs = derive_agent_status(defn, events, open_tasks=[task_in_review])
    assert (
        format_agent_status_line(status_with_prs)
        == "- Orchestrator: idle, task: Implement events, PRs: #10, #12"
    )


@pytest.mark.asyncio
async def test_sqlite_status_board_snapshot_catalog_order(databases: Databases) -> None:
    project_id = "prj_statustest"
    hub = EventHub()
    events = SqliteEventLog(databases, hub=hub)

    # Catalog with multiple definitions
    d_orch = make_definition("orchestrator", "Orchestrator", chat=True)
    d_retrain = make_definition("retraining", "Retraining", chat=True)
    d_alert = make_definition("alert-prioritization", "Alert Prioritization", chat=False)
    catalog = AgentCatalog([d_alert, d_orch, d_retrain], tool_names=frozenset())

    fake_sessions = FakeSessions(turn_state_by_agent={"retraining": "running"})
    board = SqliteStatusBoard(
        catalog,
        events,
        sessions=fake_sessions,
    )

    snapshot = await board.snapshot(project_id)
    assert len(snapshot) == 3
    # Catalog order: Orchestrator first, then alphabetical (alert-prioritization, retraining)
    assert [s.agent for s in snapshot] == [
        "orchestrator",
        "alert-prioritization",
        "retraining",
    ]

    assert snapshot[0].state == "idle"
    assert snapshot[1].state == "inactive"
    assert snapshot[2].state == "running"

    summary = await board.summary(project_id)
    lines = summary.split("\n")
    assert len(lines) == 3
    assert lines[0] == "- Orchestrator: idle"
    assert lines[1] == "- Alert Prioritization: inactive"
    assert lines[2] == "- Retraining: running"
    assert len(summary) <= 1500


@pytest.mark.asyncio
async def test_orchestrator_system_message_contains_summary_and_another_agent_does_not(
    databases: Databases,
    tmp_path: Path,
) -> None:
    from mlops_agents.workspace.models import Project
    from tests.agents.runtime.graph_env import prompt_messages

    project_id = "prj_statustest"
    hub = EventHub()
    events = SqliteEventLog(databases, hub=hub)

    # Orchestrator has status_board=True; retraining has status_board=False
    d_orch = make_definition("orchestrator", "Orchestrator", chat=True, status_board=True)
    d_retrain = make_definition("retraining", "Retraining", chat=True, status_board=False)
    catalog = AgentCatalog([d_orch, d_retrain], tool_names=frozenset())

    fake_sessions = FakeSessions(turn_state_by_agent={"retraining": "running"})
    board = SqliteStatusBoard(
        catalog,
        events,
        sessions=fake_sessions,
    )

    summary_text = await board.summary(project_id)
    assert "- Orchestrator: idle" in summary_text
    assert "- Retraining: running" in summary_text

    project = Project(
        id=project_id,
        name="test-project",
        source="local",
        local_path=str(tmp_path / "test"),
        github_full_name=None,
        default_branch="main",
        status="ok",
        status_detail=None,
        created_at="2026-10-24T12:00:00Z",
        last_opened_at=None,
    )

    hooks = status_board_hooks(board)

    async def hook_context(definition: AgentDefinition) -> str:
        ctx = HookContext(project_id=project_id, agent=definition.name, trigger_text="")
        return await hooks.run(definition.hook_names, ctx)

    orch_context = await hook_context(d_orch)
    retrain_context = await hook_context(d_retrain)

    assert orch_context.startswith('<data source="hook:status_board">\nStatus board:\n')
    assert retrain_context == ""

    orch_msgs = prompt_messages(d_orch, project, "README.md", None, [], hook_context=orch_context)
    retrain_msgs = prompt_messages(
        d_retrain, project, "README.md", None, [], hook_context=retrain_context
    )

    orch_content = str(orch_msgs[0].content)
    retrain_content = str(retrain_msgs[0].content)

    assert "Status board:\n" in orch_content
    assert "- Orchestrator: idle" in orch_content
    assert "- Retraining: running" in orch_content

    assert "Status board:" not in retrain_content


@pytest.mark.asyncio
async def test_sqlite_status_board_separate_history_windows_preserves_prs(
    databases: Databases,
) -> None:
    project_id = "prj_windowstest"
    events = SqliteEventLog(databases)
    d_retrain = make_definition("retraining", "Retraining", chat=True)
    catalog = AgentCatalog([d_retrain], tool_names=frozenset())

    # Record 1 pr.opened event
    await events.record(
        project_id,
        source="retraining",
        type="pr.opened",
        payload={
            "task_id": "tsk_1",
            "number": 99,
            "url": "https://github.com/org/repo/pull/99",
            "title": "PR 99",
        },
    )

    # Record 150 message.sent events (which would push pr.opened out if limit were shared)
    for i in range(150):
        await events.record(
            project_id,
            source="retraining",
            type="message.sent",
            payload={"to": "orchestrator", "exchange_id": f"ex_{i}", "hop": 1},
        )

    task = TaskView(
        id="tsk_1",
        agent="retraining",
        title="Retrain model",
        branch="plan/retrain",
        status="in_review",
        review_kind="github",
        pr_number=99,
        pr_url="https://github.com/org/repo/pull/99",
    )

    board = SqliteStatusBoard(catalog, events, tasks=FakeTasks([task]))
    snapshot = await board.snapshot(project_id)
    assert len(snapshot) == 1
    # PR 99 is preserved because pr.opened has its own bounded window
    assert len(snapshot[0].open_prs) == 1
    assert snapshot[0].open_prs[0].number == 99


@pytest.mark.asyncio
async def test_sqlite_status_board_summary_cuts_at_line_boundary(databases: Databases) -> None:
    project_id = "prj_cuttest"
    events = SqliteEventLog(databases)

    # Create 50 agent definitions with long names so total summary exceeds 1500 chars
    definitions = [
        make_definition(
            f"agent-{i:02d}",
            f"Very Long Agent Display Name Number {i:02d} With Additional Descriptive Words",
            chat=True,
        )
        for i in range(50)
    ]
    catalog = AgentCatalog(definitions, tool_names=frozenset())
    board = SqliteStatusBoard(catalog, events)

    summary = await board.summary(project_id)
    assert len(summary) <= 1500
    assert summary.startswith("- ")

    # Verify every line in the summary is a complete, formatted agent line
    lines = summary.split("\n")
    assert len(lines) > 5  # Has multiple lines
    assert len(lines) < 50  # Cut before reaching all 50 agents

    for i, line in enumerate(lines):
        expected_line = format_agent_status_line(derive_agent_status(definitions[i], []))
        assert line == expected_line

    # The next agent that didn't fit is NOT partially included
    next_agent_line = format_agent_status_line(derive_agent_status(definitions[len(lines)], []))
    assert next_agent_line not in summary
    assert not summary.endswith("\n")
    assert summary.endswith(lines[-1])


def test_sqlite_status_board_bind_sessions_twice_raises(databases: Databases) -> None:
    events = SqliteEventLog(databases)
    catalog = AgentCatalog([], tool_names=frozenset())
    board = SqliteStatusBoard(catalog, events)
    fake_sessions = FakeSessions()
    board.bind_sessions(fake_sessions)
    with pytest.raises(RuntimeError, match="already bound"):
        board.bind_sessions(fake_sessions)
