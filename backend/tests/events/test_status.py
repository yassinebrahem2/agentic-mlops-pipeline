"""Tests for StatusBoard and agent status derivation (Task 3 of Stage 2 Events)."""

from __future__ import annotations

import pytest

from mlops_agents.agents.catalog import AgentCatalog, AgentDefinition
from mlops_agents.agents.runtime.contract import TaskView
from mlops_agents.events.contract import EventRecord
from mlops_agents.events.hub import EventHub
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.events.status import (
    SqliteStatusBoard,
    derive_agent_status,
    format_agent_status_line,
)
from mlops_agents.storage.databases import Databases


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
    event_type: str,
    source: str = "orchestrator",
    subject_id: str | None = None,
    payload: dict[str, object] | None = None,
) -> EventRecord:
    return EventRecord(
        id=f"evt_{seq}",
        seq=seq,
        ts="2026-10-24T12:00:00Z",
        source=source,
        type=event_type,  # pyright: ignore[reportArgumentType]
        subject_id=subject_id,
        payload=payload or {},
    )


def test_derive_agent_status_inactive_when_chat_false() -> None:
    defn = make_definition(
        name="alert-prioritization", display_name="Alert Prioritization", chat=False
    )
    # Even if there are events, chat=False is always inactive
    events = [
        make_event(1, "turn.started", source="alert-prioritization", subject_id="trn_1"),
    ]
    status = derive_agent_status(defn, events)
    assert status.state == "inactive"
    assert status.agent == "alert-prioritization"
    assert status.display_name == "Alert Prioritization"


def test_derive_agent_status_running() -> None:
    defn = make_definition()
    events = [
        make_event(1, "turn.started", source="orchestrator", subject_id="trn_1"),
    ]
    status = derive_agent_status(defn, events)
    assert status.state == "running"
    assert status.last_finished is None


def test_derive_agent_status_waiting_approval() -> None:
    defn = make_definition()
    events = [
        make_event(1, "turn.started", source="orchestrator", subject_id="trn_1"),
        make_event(2, "turn.waiting", source="orchestrator", subject_id="trn_1"),
    ]
    status = derive_agent_status(defn, events)
    assert status.state == "waiting_approval"


def test_derive_agent_status_idle_and_last_finished_capped() -> None:
    defn = make_definition()
    long_final = "Result: " + "X" * 300
    events = [
        make_event(1, "turn.started", source="orchestrator", subject_id="trn_1"),
        make_event(
            2,
            "turn.ended",
            source="orchestrator",
            subject_id="trn_1",
            payload={"final_text": long_final},
        ),
    ]
    status = derive_agent_status(defn, events)
    assert status.state == "idle"
    assert status.last_finished is not None
    assert len(status.last_finished) == 200
    assert status.last_finished == long_final[:200]


def test_derive_agent_status_waiting_reply_lifecycle() -> None:
    defn_a = make_definition(name="agent-a", display_name="Agent A")
    defn_b = make_definition(name="agent-b", display_name="Agent B")

    # 1. Agent A sends message in exchange xch_1 to Agent B
    events = [
        make_event(
            1,
            "message.sent",
            source="agent-a",
            payload={"recipient": "agent-b", "exchange": "xch_1", "hop": 1},
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
            payload={"recipient": "agent-a", "exchange": "xch_1", "hop": 2},
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
            payload={"recipient": "agent-c", "exchange": "xch_2", "hop": 1},
        )
    )
    status_a = derive_agent_status(defn_a, events)
    # The last exchange Agent A sent into is now xch_2, which has no reply yet
    assert status_a.state == "waiting_reply"


def test_derive_agent_status_open_prs_and_task_ended() -> None:
    defn = make_definition(name="retraining", display_name="Retraining")
    events = [
        make_event(
            1,
            "pr.opened",
            source="retraining",
            subject_id="tsk_1",
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
            subject_id="tsk_1",
            payload={"number": 42, "title": "Retrain model v2"},
        ),
    ]

    status = derive_agent_status(defn, events)
    assert len(status.open_prs) == 1
    assert status.open_prs[0].number == 42
    assert status.open_prs[0].title == "Retrain model v2"
    assert status.open_prs[0].url == "https://github.com/org/repo/pull/42"

    # Now task_ended occurs for tsk_1
    events.append(
        make_event(
            3,
            "task.ended",
            source="retraining",
            subject_id="tsk_1",
            payload={"task_id": "tsk_1"},
        )
    )
    status_after = derive_agent_status(defn, events)
    assert len(status_after.open_prs) == 0


def test_format_agent_status_line() -> None:
    defn = make_definition()
    status_idle = derive_agent_status(defn, [])
    assert format_agent_status_line(status_idle) == "- Orchestrator: idle"

    # With task
    task = TaskView(
        id="tsk_123",
        title="Implement events",
        branch="plan/events",
        status="active",
        review_kind=None,
        pr_number=None,
        pr_url=None,
    )
    status_with_task = derive_agent_status(defn, [], task=task)
    assert (
        format_agent_status_line(status_with_task) == "- Orchestrator: idle, task: Implement events"
    )

    # With open PRs
    events = [
        make_event(
            1,
            "pr.opened",
            source="orchestrator",
            payload={"number": 10, "url": "", "title": "PR 10"},
        ),
        make_event(
            2,
            "pr.opened",
            source="orchestrator",
            payload={"number": 12, "url": "", "title": "PR 12"},
        ),
    ]
    status_with_prs = derive_agent_status(defn, events, task=task)
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

    board = SqliteStatusBoard(catalog, events, databases=databases)

    # Record turn started for retraining
    await events.record(project_id, source="retraining", type="turn.started", payload={})

    snapshot = await board.snapshot(project_id)
    assert len(snapshot) == 3
    # Catalog order: Orchestrator first, then alphabetical (alert-prioritization, retraining)
    assert [s.agent for s in snapshot] == ["orchestrator", "alert-prioritization", "retraining"]

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
