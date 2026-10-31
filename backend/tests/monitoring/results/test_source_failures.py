"""Tests for source failures and source_failure signals (Task 4 - issue #460)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.incidents.contract import SignalDraft, SignalOutcome
from mlops_agents.monitoring.results.contract import source_id
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import FileResults, HandoffPayload, Operations
from mlops_agents.storage.databases import Databases
from tests.monitoring.results.conftest import FakeConnections, FakeHandoffs
from tests.proposals.samples import handoff as sample_handoff_dict


class FakeIncidents:
    def __init__(self) -> None:
        self.signals: list[tuple[str, SignalDraft, datetime]] = []

    async def raise_signal(
        self, project_id: str, draft: SignalDraft, seen_at: datetime
    ) -> SignalOutcome:
        self.signals.append((project_id, draft, seen_at))
        return SignalOutcome(
            signal_id=f"sig_{len(self.signals)}",
            incident_id="inc_1",
            change="opened",
            severity="medium",
        )


class FakeMaintenance:
    def __init__(self, suspended_scopes: set[str] | None = None) -> None:
        self.suspended_scopes: set[str] = suspended_scopes or set()

    async def suspended(self, project_id: str, scope: Any, at: datetime) -> bool:
        return str(scope) in self.suspended_scopes


def _build_handoff(conn_name: str = "conn_err", pattern: str = "*.jsonl") -> HandoffView:
    ops = Operations(
        scheduler=None,
        feeds=[],
        on_call=[],
        checks=[],
        results_location=FileResults(connection=conn_name, path_pattern=pattern),
    )
    payload = HandoffPayload.model_validate(sample_handoff_dict(operations=ops.model_dump()))
    return HandoffView(
        proposal_id="prop_err",
        version=1,
        approved=datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC),
        payload=payload,
    )


async def test_unreadable_source_raises_source_failure_signal_and_keeps_cursor(
    databases: Databases, events: SqliteEventLog, project_id: str
) -> None:
    # Connection points to non-existent directory
    connections = FakeConnections()
    connections.add_local_folder("conn_err", Path("non_existent_folder_xyz"))

    handoffs = FakeHandoffs(_build_handoff("conn_err", "*.jsonl"))
    incidents = FakeIncidents()
    maintenance = FakeMaintenance()

    service = ResultsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        events=events,
        incidents=incidents,
        maintenance=maintenance,
    )

    s_id = source_id("file", "*.jsonl", "conn_err")
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)

    rep = await service.collect(project_id, now)
    assert s_id in rep.failures

    # Verify source state kept cursor and recorded error
    st = await service.store.get_source_state(project_id, s_id)
    assert st is not None
    assert st.cursor == {}
    assert st.consecutive_failures == 1
    assert st.last_error is not None

    # Verify source_failure signal was raised through Incidents
    assert len(incidents.signals) == 1
    p_id, draft, seen_at = incidents.signals[0]
    assert p_id == project_id
    assert draft.kind == "source_failure"
    assert draft.category == "operational"
    assert draft.subject == s_id
    assert draft.source_ref == s_id
    assert draft.ran_at == now
    assert draft.detail["source_id"] == s_id
    assert draft.detail["error"] == st.last_error
    assert seen_at == now


async def test_invalid_file_line_raises_source_failure_signal_and_moves_cursor_past_it(
    databases: Databases, events: SqliteEventLog, project_id: str, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "results.jsonl"

    valid_1 = (
        b'{"check_id": "c1", "origin": "agent", "ran_at": "2026-10-30T10:00:00Z", '
        b'"fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    )
    invalid_line = b'{"bad_json": true\n'  # Invalid JSON syntax
    valid_2 = (
        b'{"check_id": "c2", "origin": "agent", "ran_at": "2026-10-30T10:05:00Z", '
        b'"fired": true, "category": "operational", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    )
    full_content = valid_1 + invalid_line + valid_2
    target.write_bytes(full_content)

    connections = FakeConnections()
    connections.add_local_folder("conn_valid", data_dir)
    handoffs = FakeHandoffs(_build_handoff("conn_valid", "*.jsonl"))
    incidents = FakeIncidents()
    maintenance = FakeMaintenance()

    service = ResultsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        events=events,
        incidents=incidents,
        maintenance=maintenance,
    )

    s_id = source_id("file", "*.jsonl", "conn_valid")
    now = datetime(2026, 10, 30, 10, 10, 0, tzinfo=UTC)

    rep = await service.collect(project_id, now)
    # Both valid lines stored
    assert rep.stored == 2
    # Invalid line recorded as failure
    assert s_id in rep.failures

    # Cursor moved past all lines (including the invalid line)
    st = await service.store.get_source_state(project_id, s_id)
    assert st is not None
    assert st.cursor["files"]["results.jsonl"]["offset"] == len(full_content)

    # Signal was raised for the invalid line
    assert len(incidents.signals) == 1
    assert incidents.signals[0][1].kind == "source_failure"
    assert incidents.signals[0][1].subject == s_id


async def test_suspended_connection_raises_no_signal_on_source_failure(
    databases: Databases, events: SqliteEventLog, project_id: str
) -> None:
    connections = FakeConnections()
    connections.add_local_folder("conn_suspended", Path("non_existent_folder_xyz"))

    handoffs = FakeHandoffs(_build_handoff("conn_suspended", "*.jsonl"))
    incidents = FakeIncidents()
    # Maintenance suspends scope="conn_suspended"
    maintenance = FakeMaintenance(suspended_scopes={"conn_suspended"})

    service = ResultsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        events=events,
        incidents=incidents,
        maintenance=maintenance,
    )

    s_id = source_id("file", "*.jsonl", "conn_suspended")
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)

    rep = await service.collect(project_id, now)
    assert s_id in rep.failures

    # Source error recorded in DB
    st = await service.store.get_source_state(project_id, s_id)
    assert st is not None
    assert st.last_error is not None

    # But NO signal was raised because connection is suspended!
    assert len(incidents.signals) == 0
