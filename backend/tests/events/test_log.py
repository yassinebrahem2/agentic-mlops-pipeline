"""Tests for SqliteEventLog (I11)."""

from __future__ import annotations

import pytest

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.storage.databases import Databases


@pytest.mark.asyncio
async def test_event_log_record_and_history(databases: Databases) -> None:
    project_id = "prj_test1"
    event_log = SqliteEventLog(databases)

    # Record 5 events of various types
    e1 = await event_log.record(project_id, source="user", type="turn.started", payload={"turn": 1})
    await event_log.record(
        project_id, source="orchestrator", type="message.sent", payload={"msg": "hi"}
    )
    await event_log.record(
        project_id, source="orchestrator", type="turn.ended", payload={"turn": 1, "status": "done"}
    )
    await event_log.record(project_id, source="user", type="turn.started", payload={"turn": 2})
    e5 = await event_log.record(
        project_id, source="orchestrator", type="turn.ended", payload={"turn": 2, "status": "done"}
    )

    assert e1.seq == 1
    assert e1.id.startswith("evt_")
    assert e5.seq == 5

    # History: newest first, filtered by type
    history = await event_log.history(project_id, types=["turn.started", "turn.ended"], limit=3)
    assert len(history) == 3
    # Newest first: e5, e4, e3
    assert [e.seq for e in history] == [5, 4, 3]
    assert [e.type for e in history] == ["turn.ended", "turn.started", "turn.ended"]

    # History: empty types returns empty list
    empty_history = await event_log.history(project_id, types=[], limit=10)
    assert empty_history == []

    # History for nonexistent project returns empty list
    other_history = await event_log.history("prj_other1", types=["turn.started"], limit=10)
    assert other_history == []
