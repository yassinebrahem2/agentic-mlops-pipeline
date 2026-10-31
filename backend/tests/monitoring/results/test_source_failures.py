"""Tests for source failures and source_failure signals (Task 4 - issue #460)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.incidents.contract import SignalDraft, SignalOutcome
from mlops_agents.monitoring.results.contract import source_id
from mlops_agents.monitoring.results.service import ResultsService, SourceReadOutcome
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


async def test_consecutive_failing_ticks_raise_one_signal_until_recovery_and_new_failure(
    databases: Databases, events: SqliteEventLog, project_id: str, tmp_path: Path
) -> None:
    data_dir = tmp_path / "flaky_data"
    # Initially directory does not exist
    connections = FakeConnections()
    connections.add_local_folder("conn_flaky", data_dir)
    handoffs = FakeHandoffs(_build_handoff("conn_flaky", "*.jsonl"))
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
    s_id = source_id("file", "*.jsonl", "conn_flaky")

    # Tick 1: failing (directory missing) -> raises signal 1
    t1 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
    rep1 = await service.collect(project_id, t1)
    assert s_id in rep1.failures
    assert len(incidents.signals) == 1
    st1 = await service.store.get_source_state(project_id, s_id)
    assert st1 is not None
    assert st1.consecutive_failures == 1
    assert st1.last_error is not None

    # Tick 2: still failing -> NO new signal raised
    t2 = datetime(2026, 10, 30, 10, 5, 0, tzinfo=UTC)
    rep2 = await service.collect(project_id, t2)
    assert s_id in rep2.failures
    assert len(incidents.signals) == 1
    st2 = await service.store.get_source_state(project_id, s_id)
    assert st2 is not None
    assert st2.consecutive_failures == 2

    # Tick 3: directory created and valid file added -> recovery!
    data_dir.mkdir(parents=True, exist_ok=True)
    valid_file = data_dir / "results.jsonl"
    valid_file.write_bytes(
        b'{"check_id": "c1", "origin": "agent", "ran_at": "2026-10-30T10:10:00Z", '
        b'"fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    )
    t3 = datetime(2026, 10, 30, 10, 10, 0, tzinfo=UTC)
    rep3 = await service.collect(project_id, t3)
    assert s_id not in rep3.failures
    assert rep3.stored == 1
    assert len(incidents.signals) == 1
    st3 = await service.store.get_source_state(project_id, s_id)
    assert st3 is not None
    assert st3.consecutive_failures == 0
    assert st3.last_error is None

    # Tick 4: directory removed -> failure again -> raises signal 2!
    for f in data_dir.iterdir():
        f.unlink()
    data_dir.rmdir()

    t4 = datetime(2026, 10, 30, 10, 15, 0, tzinfo=UTC)
    rep4 = await service.collect(project_id, t4)
    assert s_id in rep4.failures
    assert len(incidents.signals) == 2
    assert incidents.signals[1][1].kind == "source_failure"
    st4 = await service.store.get_source_state(project_id, s_id)
    assert st4 is not None
    assert st4.consecutive_failures == 1
    assert st4.last_error is not None


async def test_failing_source_scrubs_url_credentials_from_error_signal_and_logs(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_pass = "super_secret_token_12345"
    secret_param = "query_secret_token_99999"
    raw_error = f"Connection failed to https://admin:{secret_pass}@mlops.internal/api/v1?token={secret_param}&format=json"

    class FailingReader:
        async def read(
            self,
            project_id: str,
            source_id: str,
            cursor: dict[str, Any],
            *,
            now: datetime,
            consecutive_failures: int = 0,
        ) -> SourceReadOutcome:
            raise RuntimeError(raw_error)

    connections = FakeConnections()
    handoffs = FakeHandoffs(_build_handoff("conn_sec", "*.jsonl"))
    incidents = FakeIncidents()
    maintenance = FakeMaintenance()

    service = ResultsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        events=events,
        incidents=incidents,
        maintenance=maintenance,
        file_reader=FailingReader(),  # type: ignore[arg-type]
    )

    s_id = source_id("file", "*.jsonl", "conn_sec")
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)

    with caplog.at_level(logging.WARNING):
        rep = await service.collect(project_id, now)

    assert s_id in rep.failures
    st = await service.store.get_source_state(project_id, s_id)
    assert st is not None
    assert st.last_error is not None

    # Verify secrets are scrubbed from DB state
    assert secret_pass not in st.last_error
    assert secret_param not in st.last_error
    assert "***@" in st.last_error

    # Verify secrets are scrubbed from SignalDraft detail
    assert len(incidents.signals) == 1
    draft = incidents.signals[0][1]
    assert secret_pass not in str(draft.detail)
    assert secret_param not in str(draft.detail)
    assert secret_pass not in draft.detail["error"]
    assert secret_param not in draft.detail["error"]

    # Verify secrets are not present in logs
    assert secret_pass not in caplog.text
    assert secret_param not in caplog.text
