"""Tests for source failures and source_failure signals (Task 4 - issue #460)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mlops_agents.connections.contract import Mount
from mlops_agents.connections.kinds import DATA_MOUNT_ROOT
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.expr import Compare
from mlops_agents.monitoring.incidents.contract import SignalDraft, SignalOutcome
from mlops_agents.monitoring.results.code_parser import CodeParserRunner
from mlops_agents.monitoring.results.contract import (
    ParserCode,
    ParserMapping,
    ParserSpec,
    ResultQuery,
    ResultRecord,
    SourceState,
    source_id,
    waiting_for_parser_error,
)
from mlops_agents.monitoring.results.parsers import SqliteParsers
from mlops_agents.monitoring.results.service import ResultsService, SourceReadOutcome
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import (
    Cadence,
    CheckSpec,
    FileResults,
    HandoffPayload,
    Operations,
)
from mlops_agents.storage.databases import Databases
from tests.monitoring.results.conftest import (
    FakeConnections,
    FakeHandoffs,
    FakeSandbox,
    LocalExecDockerRunner,
    MountsConnections,
)
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
            is_results_location: bool = False,
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


async def test_team_lines_without_parser_leave_cursor_and_reread_when_parser_approved(
    databases: Databases, events: SqliteEventLog, project_id: str, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "results.jsonl"

    team_line_1 = (
        b'{"check_id": "c1", "ran_at": "2026-10-30T10:00:00Z", '
        b'"fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    )
    team_line_2 = (
        b'{"check_id": "c2", "ran_at": "2026-10-30T10:05:00Z", '
        b'"fired": true, "category": "operational", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    )
    full_content = team_line_1 + team_line_2
    target.write_bytes(full_content)

    from mlops_agents.proposals.payloads import Cadence, CheckSpec

    connections = FakeConnections()
    connections.add_local_folder("conn_team", data_dir)
    ops = Operations(
        scheduler=None,
        feeds=[],
        on_call=[],
        checks=[
            CheckSpec(
                check_id="c1",
                name="Team check",
                category="data_quality",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="conn_team", path_pattern="*.jsonl"),
            )
        ],
        results_location=FileResults(connection="conn_team", path_pattern="agent_results/*.jsonl"),
    )
    payload = HandoffPayload.model_validate(sample_handoff_dict(operations=ops.model_dump()))
    handoffs = FakeHandoffs(
        HandoffView(
            proposal_id="prop_team",
            version=1,
            approved=datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC),
            payload=payload,
        )
    )
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

    s_id = source_id("file", "*.jsonl", "conn_team")
    now = datetime(2026, 10, 30, 10, 10, 0, tzinfo=UTC)

    # 1. Team check source without a parser is waiting for a parser (reads nothing).
    rep = await service.collect(project_id, now)
    assert rep.stored == 0

    st = await service.store.get_source_state(project_id, s_id)
    assert st is not None
    assert st.cursor == {}

    # 2. Approving a parser for that source resets cursor and stores every earlier record once.
    spec = ParserSpec(
        source_id=s_id,
        mapping=ParserMapping(
            format="jsonl",
            fields={"check_id": "check_id", "ran_at": "ran_at"},
            fired_when=Compare(op="eq", field="fired", value=True),
            defaults={"category": "operational", "subject": "s"},
        ),
    )
    await service.parsers.store(project_id, spec, "prop_team_1")

    # The parser store reset cursor to "{}" in result_sources
    st_after_store = await service.store.get_source_state(project_id, s_id)
    assert st_after_store is not None
    assert st_after_store.cursor == {}

    rep2 = await service.collect(project_id, now + timedelta(minutes=5))
    assert rep2.stored == 2
    assert s_id not in rep2.failures

    st2 = await service.store.get_source_state(project_id, s_id)
    assert st2 is not None
    assert st2.cursor["files"]["results.jsonl"]["offset"] == len(full_content)

    # 3. A second read stores nothing new (stored: 0, cursor remains at EOF).
    rep3 = await service.collect(project_id, now + timedelta(minutes=10))
    assert rep3.stored == 0
    st3 = await service.store.get_source_state(project_id, s_id)
    assert st3 is not None
    assert st3.cursor["files"]["results.jsonl"]["offset"] == len(full_content)


@pytest.mark.asyncio
async def test_results_service_source_state(databases: Databases, project_id: str) -> None:
    events = SqliteEventLog(databases)
    handoffs = FakeHandoffs()
    connections = FakeConnections()
    service = ResultsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        events=events,
    )
    s_id = "file:conn_test:*.json"
    # Initially None
    st = await service.source_state(project_id, s_id)
    assert st is None

    # After saving source state via store
    saved = SourceState(
        source_id=s_id,
        kind="file",
        cursor={"files": {}},
        last_read=datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC),
        last_error=None,
        consecutive_failures=0,
    )
    await service.store.save_source_state(project_id, saved)

    # Now returns the SourceState
    st = await service.source_state(project_id, s_id)
    assert st is not None
    assert st.source_id == s_id
    assert st.consecutive_failures == 0
    assert st.last_error is None


@pytest.mark.asyncio
async def test_mixed_check_source_reread_stores_each_record_once_and_preserves_agent_origin(
    databases: Databases,
    project_id: str,
    tmp_path: Path,
) -> None:
    events = SqliteEventLog(databases)
    data_dir = tmp_path / "mixed_check_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "results.jsonl"

    # Line 1: agent record with explicit custom source_key
    agent_line_1 = (
        b'{"check_id": "c1", "origin": "agent", "source_key": "agent-key-1", '
        b'"ran_at": "2026-10-30T10:00:00Z", "fired": false, "category": "data_quality", '
        b'"subject": "s", "detail": {}, "source_ref": "results.jsonl"}\n'
    )
    # Line 2: agent record without custom source_key (key derived from rel_posix:offset)
    agent_line_2 = (
        b'{"check_id": "c2", "origin": "agent", '
        b'"ran_at": "2026-10-30T10:05:00Z", "fired": true, "category": "operational", '
        b'"subject": "s", "detail": {}, "source_ref": "results.jsonl"}\n'
    )
    # Line 3: team record without origin: "agent"
    team_line_3 = (
        b'{"check_id": "c3", "ran_at": "2026-10-30T10:10:00Z", '
        b'"fired": false, "category": "model_performance", "subject": "s", "detail": {}, "source_ref": "results.jsonl"}\n'
    )
    full_content = agent_line_1 + agent_line_2 + team_line_3
    target.write_bytes(full_content)

    connections = FakeConnections()
    connections.add_local_folder("conn_mixed", data_dir)
    ops = Operations(
        scheduler=None,
        feeds=[],
        on_call=[],
        checks=[
            CheckSpec(
                check_id="c1",
                name="Mixed check",
                category="data_quality",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="conn_mixed", path_pattern="*.jsonl"),
            )
        ],
        results_location=FileResults(connection="conn_mixed", path_pattern="agent_results/*.jsonl"),
    )
    payload = HandoffPayload.model_validate(sample_handoff_dict(operations=ops.model_dump()))
    handoffs = FakeHandoffs(
        HandoffView(
            proposal_id="prop_mixed",
            version=1,
            approved=datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC),
            payload=payload,
        )
    )

    service = ResultsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        events=events,
    )

    s_id = source_id("file", "*.jsonl", "conn_mixed")
    now = datetime(2026, 10, 30, 10, 15, 0, tzinfo=UTC)

    # 1. Before parser: source is waiting for a parser (reads nothing, cursor empty)
    rep1 = await service.collect(project_id, now)
    assert rep1.stored == 0
    st1 = await service.store.get_source_state(project_id, s_id)
    assert st1 is not None
    assert st1.cursor == {}

    # Pre-existing agent records stored earlier (from agent check or prior run)
    rec_c1 = ResultRecord(
        check_id="c1",
        origin="agent",
        ran_at=datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC),
        fired=False,
        category="data_quality",
        subject="s",
        detail={},
        source_ref="results.jsonl",
    )
    rec_c2 = ResultRecord(
        check_id="c2",
        origin="agent",
        ran_at=datetime(2026, 10, 30, 10, 5, 0, tzinfo=UTC),
        fired=True,
        category="operational",
        subject="s",
        detail={},
        source_ref="results.jsonl",
    )
    await service.store.insert_items(
        project_id,
        s_id,
        [("agent-key-1", rec_c1), (f"results.jsonl:{len(agent_line_1)}", rec_c2)],
        now,
    )

    # Verify the 2 stored records both have origin "agent"
    stored_c1 = await service.store.query(project_id, ResultQuery(check_id="c1"))
    assert len(stored_c1.rows) == 1
    assert stored_c1.rows[0].origin == "agent"
    assert stored_c1.rows[0].source_key == "agent-key-1"

    stored_c2 = await service.store.query(project_id, ResultQuery(check_id="c2"))
    assert len(stored_c2.rows) == 1
    assert stored_c2.rows[0].origin == "agent"
    assert stored_c2.rows[0].source_key == f"results.jsonl:{len(agent_line_1)}"

    # 2. Store parser proposal for the source (resets cursor to {})
    spec = ParserSpec(
        source_id=s_id,
        mapping=ParserMapping(
            format="jsonl",
            fields={"check_id": "check_id", "ran_at": "ran_at"},
            fired_when=Compare(op="eq", field="fired", value=True),
            defaults={"category": "operational", "subject": "s"},
        ),
    )
    await service.parsers.store(project_id, spec, "prop_mixed_parser")
    st_after = await service.store.get_source_state(project_id, s_id)
    assert st_after is not None
    assert st_after.cursor == {}

    # 3. Second collect (with parser):
    # c1 and c2 are recognized as agent origin, keys match earlier records -> skipped by dedup.
    # c3 is parsed through mapping as origin "team" and inserted.
    rep2 = await service.collect(project_id, now + timedelta(minutes=5))
    assert rep2.stored == 1
    assert s_id not in rep2.failures
    st2 = await service.store.get_source_state(project_id, s_id)
    assert st2 is not None
    assert st2.cursor["files"]["results.jsonl"]["offset"] == len(full_content)

    # Total rows in results table for project is 3
    all_res = await service.store.query(project_id, ResultQuery(limit=50))
    assert len(all_res.rows) == 3

    # Check origin of all records: c1 and c2 remain agent, c3 is team
    res_by_check = {r.check_id: r for r in all_res.rows}
    assert res_by_check["c1"].origin == "agent"
    assert res_by_check["c1"].source_key == "agent-key-1"
    assert res_by_check["c2"].origin == "agent"
    assert res_by_check["c2"].source_key == f"results.jsonl:{len(agent_line_1)}"
    assert res_by_check["c3"].origin == "team"
    assert res_by_check["c3"].source_key == f"results.jsonl:{len(agent_line_1) + len(agent_line_2)}"

    # 4. Third collect: cursor is at EOF, 0 stored
    rep3 = await service.collect(project_id, now + timedelta(minutes=10))
    assert rep3.stored == 0
    all_res3 = await service.store.query(project_id, ResultQuery(limit=50))
    assert len(all_res3.rows) == 3


@pytest.mark.asyncio
async def test_mixed_check_source_code_parser_reread_stores_each_record_once_and_preserves_agent_origin(
    databases: Databases,
    project_id: str,
    tmp_path: Path,
) -> None:
    events = SqliteEventLog(databases)
    data_dir = tmp_path / "mixed_code_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "results.jsonl"

    # Line 1: agent record with explicit custom source_key
    agent_line_1 = (
        b'{"check_id": "c1", "origin": "agent", "source_key": "K1", '
        b'"ran_at": "2026-10-30T10:00:00Z", "fired": false, "category": "data_quality", '
        b'"subject": "s", "detail": {}, "source_ref": "results.jsonl"}\n'
    )
    # Line 2: agent record without custom source_key
    agent_line_2 = (
        b'{"check_id": "c2", "origin": "agent", '
        b'"ran_at": "2026-10-30T10:05:00Z", "fired": true, "category": "operational", '
        b'"subject": "s", "detail": {}, "source_ref": "results.jsonl"}\n'
    )
    # Line 3: team record without origin: "agent"
    team_line_3 = (
        b'{"check_id": "c3", "ran_at": "2026-10-30T10:10:00Z", '
        b'"fired": false, "category": "model_performance", "subject": "s", "detail": {}, "source_ref": "results.jsonl"}\n'
    )
    full_content = agent_line_1 + agent_line_2 + team_line_3
    target.write_bytes(full_content)

    mount = Mount(host_path=data_dir, container_path=f"{DATA_MOUNT_ROOT}/conn_mixed_code")
    connections = MountsConnections([mount])
    connections.add_local_folder("conn_mixed_code", data_dir)
    docker_runner = LocalExecDockerRunner("sandbox:test-tag", [mount])
    runner = CodeParserRunner(
        sandbox=FakeSandbox(tag="sandbox:test-tag"),
        docker_runner=docker_runner,
        connections=connections,
    )
    parsers = SqliteParsers(databases, code_runner=runner)

    ops = Operations(
        scheduler=None,
        feeds=[],
        on_call=[],
        checks=[
            CheckSpec(
                check_id="c1",
                name="Mixed code check",
                category="data_quality",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="conn_mixed_code", path_pattern="*.jsonl"),
            )
        ],
        results_location=FileResults(
            connection="conn_mixed_code", path_pattern="agent_results/*.jsonl"
        ),
    )
    payload = HandoffPayload.model_validate(sample_handoff_dict(operations=ops.model_dump()))
    handoffs = FakeHandoffs(
        HandoffView(
            proposal_id="prop_mixed_code",
            version=1,
            approved=datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC),
            payload=payload,
        )
    )

    service = ResultsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        events=events,
        parsers=parsers,
    )

    s_id = source_id("file", "*.jsonl", "conn_mixed_code")
    now = datetime(2026, 10, 30, 10, 15, 0, tzinfo=UTC)

    # 1. Before parser: source is waiting for a parser (reads nothing, cursor empty)
    rep1 = await service.collect(project_id, now)
    assert rep1.stored == 0
    st1 = await service.store.get_source_state(project_id, s_id)
    assert st1 is not None
    assert st1.cursor == {}

    # Pre-existing agent records stored earlier (from agent check or prior run)
    rec_c1 = ResultRecord(
        check_id="c1",
        origin="agent",
        ran_at=datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC),
        fired=False,
        category="data_quality",
        subject="s",
        detail={},
        source_ref="results.jsonl",
    )
    rec_c2 = ResultRecord(
        check_id="c2",
        origin="agent",
        ran_at=datetime(2026, 10, 30, 10, 5, 0, tzinfo=UTC),
        fired=True,
        category="operational",
        subject="s",
        detail={},
        source_ref="results.jsonl",
    )
    await service.store.insert_items(
        project_id,
        s_id,
        [("K1", rec_c1), (f"results.jsonl:{len(agent_line_1)}", rec_c2)],
        now,
    )

    stored_c1 = await service.store.query(project_id, ResultQuery(check_id="c1"))
    assert len(stored_c1.rows) == 1
    assert stored_c1.rows[0].origin == "agent"
    assert stored_c1.rows[0].source_key == "K1"

    stored_c2 = await service.store.query(project_id, ResultQuery(check_id="c2"))
    assert len(stored_c2.rows) == 1
    assert stored_c2.rows[0].origin == "agent"
    assert stored_c2.rows[0].source_key == f"results.jsonl:{len(agent_line_1)}"

    # 2. Store code parser that re-emits each line without origin
    re_emit_script = """
import json, sys
for line in sys.stdin.read().splitlines():
    line = line.strip()
    if not line:
        continue
    data = json.loads(line)
    data.pop("origin", None)
    print(json.dumps(data))
"""
    spec = ParserSpec(
        source_id=s_id,
        code=ParserCode(script=re_emit_script, timeout_s=60),
    )
    await service.parsers.store(project_id, spec, "prop_reemit_parser")
    st_after = await service.store.get_source_state(project_id, s_id)
    assert st_after is not None
    assert st_after.cursor == {}

    # 3. Second collect (with code parser):
    # Agent lines c1 and c2 are diverted and never reach the script; deduplicated against existing rows.
    # The script sees only t1, re-emits it without origin, and it is stored as origin "team".
    rep2 = await service.collect(project_id, now + timedelta(minutes=5))
    assert rep2.stored == 1
    assert s_id not in rep2.failures
    st2 = await service.store.get_source_state(project_id, s_id)
    assert st2 is not None
    assert st2.cursor["files"]["results.jsonl"]["offset"] == len(full_content)

    # Total rows in results table is exactly 3: c1 (agent, K1), c2 (agent, results.jsonl:len(c1)), c3 (team)
    all_res = await service.store.query(project_id, ResultQuery(limit=50))
    assert len(all_res.rows) == 3

    res_by_check = {r.check_id: r for r in all_res.rows}
    assert res_by_check["c1"].origin == "agent"
    assert res_by_check["c1"].source_key == "K1"
    assert res_by_check["c2"].origin == "agent"
    assert res_by_check["c2"].source_key == f"results.jsonl:{len(agent_line_1)}"
    assert res_by_check["c3"].origin == "team"
    assert (
        res_by_check["c3"].source_key == f"results.jsonl:{len(agent_line_1) + len(agent_line_2)}:0"
    )

    # 4. Third collect: cursor is at EOF, 0 stored
    rep3 = await service.collect(project_id, now + timedelta(minutes=10))
    assert rep3.stored == 0
    all_res3 = await service.store.query(project_id, ResultQuery(limit=50))
    assert len(all_res3.rows) == 3


def _build_handoff_with_check(
    conn_name: str,
    pattern: str,
    check_pattern: str = "dq/*.jsonl",
    check_conn: str | None = None,
) -> HandoffView:
    ops = Operations(
        scheduler=None,
        feeds=[],
        on_call=[],
        checks=[
            CheckSpec(
                check_id="chk_team",
                name="Team Check",
                category="data_quality",
                cadence=Cadence(cron=None, every_minutes=60),
                timezone="UTC",
                results=FileResults(connection=check_conn or conn_name, path_pattern=check_pattern),
            )
        ],
        results_location=FileResults(connection=conn_name, path_pattern=pattern),
    )
    payload = HandoffPayload.model_validate(sample_handoff_dict(operations=ops.model_dump()))
    return HandoffView(
        proposal_id="prop_wait",
        version=1,
        approved=datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC),
        payload=payload,
    )


async def test_source_waiting_for_parser_stores_waiting_error_and_clears_when_parser_enabled(
    databases: Databases, events: SqliteEventLog, project_id: str, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "metrics.jsonl"
    target.write_bytes(b'{"check_id": "c1", "ran_at": "2026-10-30T10:00:00Z", "fired": true}\n')

    connections = FakeConnections()
    connections.add_local_folder("conn_team", data_dir)
    handoffs = FakeHandoffs(
        _build_handoff_with_check("conn_team", "agents/*.jsonl", check_pattern="metrics.jsonl")
    )
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

    s_id = source_id("file", "metrics.jsonl", "conn_team")
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)

    # 1. First collection: source needs a parser, none is enabled.
    rep1 = await service.collect(project_id, now)
    assert rep1.stored == 0
    assert s_id not in rep1.failures
    assert len(incidents.signals) == 0

    st1 = await service.store.get_source_state(project_id, s_id)
    assert st1 is not None
    assert st1.last_error == waiting_for_parser_error(s_id)
    assert st1.consecutive_failures == 0
    assert st1.cursor == {}

    # 2. Second collection: keeps the waiting error and raises no signal.
    rep2 = await service.collect(project_id, now + timedelta(minutes=1))
    assert rep2.stored == 0
    assert s_id not in rep2.failures
    assert len(incidents.signals) == 0

    st2 = await service.store.get_source_state(project_id, s_id)
    assert st2 is not None
    assert st2.last_error == waiting_for_parser_error(s_id)
    assert st2.consecutive_failures == 0

    # 3. Store an enabled parser for that source.
    spec = ParserSpec(
        source_id=s_id,
        mapping=ParserMapping(
            format="jsonl",
            fields={"check_id": "check_id", "ran_at": "ran_at"},
            fired_when=Compare(op="eq", field="fired", value=True),
            defaults={"category": "operational", "subject": "s"},
        ),
    )
    await service.parsers.store(project_id, spec, "prop_parser_1")

    # 4. Third collection: parser is now enabled; reads record and clears waiting error.
    rep3 = await service.collect(project_id, now + timedelta(minutes=2))
    assert rep3.stored == 1
    assert s_id not in rep3.failures
    assert len(incidents.signals) == 0

    st3 = await service.store.get_source_state(project_id, s_id)
    assert st3 is not None
    assert st3.last_error is None
    assert st3.consecutive_failures == 0


async def test_first_real_read_failure_after_waiting_for_parser_raises_source_failure(
    databases: Databases, events: SqliteEventLog, project_id: str, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    connections = FakeConnections()
    connections.add_local_folder("conn_ok", data_dir)
    connections.add_local_folder("conn_fail", Path("non_existent_folder_xyz"))

    handoff = _build_handoff_with_check(
        "conn_ok", "agents/*.jsonl", check_conn="conn_fail", check_pattern="metrics.jsonl"
    )
    handoffs = FakeHandoffs(handoff)
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

    s_id = source_id("file", "metrics.jsonl", "conn_fail")
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)

    # 1. First collection: source needs a parser, none is enabled.
    rep1 = await service.collect(project_id, now)
    assert rep1.stored == 0
    assert len(incidents.signals) == 0

    st1 = await service.store.get_source_state(project_id, s_id)
    assert st1 is not None
    assert st1.last_error == waiting_for_parser_error(s_id)
    assert st1.consecutive_failures == 0

    # 2. Store an enabled parser for that source.
    spec = ParserSpec(
        source_id=s_id,
        mapping=ParserMapping(
            format="jsonl",
            fields={"check_id": "check_id", "ran_at": "ran_at"},
            fired_when=Compare(op="eq", field="fired", value=True),
            defaults={"category": "operational", "subject": "s"},
        ),
    )
    await service.parsers.store(project_id, spec, "prop_parser_fail")

    # 3. Next collection: parser is approved, but the file read fails.
    # Because consecutive_failures was 0 (from waiting), this is the first real read failure!
    # It must raise exactly one source_failure signal!
    rep2 = await service.collect(project_id, now + timedelta(minutes=1))
    assert s_id in rep2.failures
    assert len(incidents.signals) == 1
    p_id, draft, _seen_at = incidents.signals[0]
    assert p_id == project_id
    assert draft.kind == "source_failure"
    assert draft.subject == s_id

    st2 = await service.store.get_source_state(project_id, s_id)
    assert st2 is not None
    assert st2.consecutive_failures == 1

    # 4. Third collection: second real read failure.
    # Must NOT raise another source_failure signal (deduplicated).
    rep3 = await service.collect(project_id, now + timedelta(minutes=2))
    assert s_id in rep3.failures
    assert len(incidents.signals) == 1
