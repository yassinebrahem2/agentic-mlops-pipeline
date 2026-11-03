"""Tests for parser source_id validation against project sources (Issue #738)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.expr import Compare
from mlops_agents.monitoring.results.contract import (
    ParserMapping,
    ParserPayload,
    ParserSpec,
)
from mlops_agents.monitoring.results.parser_kind import ParserKind
from mlops_agents.monitoring.results.parsers import SqliteParsers
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import (
    Cadence,
    CheckSpec,
    FileResults,
    HandoffPayload,
    Operations,
)
from mlops_agents.storage.databases import Databases
from tests.monitoring.results.conftest import FakeConnections, FakeHandoffs
from tests.proposals.samples import handoff as sample_handoff_dict


def _make_payload(src_id: str) -> ParserPayload:
    return ParserPayload(
        spec=ParserSpec(
            source_id=src_id,
            mapping=ParserMapping(
                format="jsonl",
                fields={"check_id": "id", "ran_at": "ts"},
                fired_when=Compare(op="eq", field="fired", value=True),
                defaults={},
            ),
        ),
        sample_input="",
        sample_records=[],
    )


def _build_handoff_with_checks_and_loc(
    team_conn: str,
    team_pattern: str,
    loc_conn: str,
    loc_pattern: str,
) -> HandoffView:
    ops = Operations(
        scheduler=None,
        feeds=[],
        on_call=[],
        checks=[
            CheckSpec(
                check_id="team_chk",
                name="Team Check",
                category="data_quality",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection=team_conn, path_pattern=team_pattern),
            )
        ],
        results_location=FileResults(connection=loc_conn, path_pattern=loc_pattern),
    )
    return HandoffView(
        proposal_id="prop_h1",
        version=1,
        approved=datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC),
        payload=HandoffPayload.model_validate(sample_handoff_dict(operations=ops.model_dump())),
    )


def test_parser_spec_source_id_has_description() -> None:
    desc = ParserSpec.model_fields["source_id"].description
    assert desc is not None
    assert "file:<connection>:<pattern>" in desc
    assert "refusal lists the valid ones" in desc


@pytest.mark.asyncio
async def test_team_check_file_results_derived_as_needing_parser(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    connections = FakeConnections()
    connections.add_local_folder("conn_team", data_dir)
    connections.add_local_folder("conn_loc", data_dir)

    handoff = _build_handoff_with_checks_and_loc(
        team_conn="conn_team",
        team_pattern="checks/*.jsonl",
        loc_conn="conn_loc",
        loc_pattern="results/*.jsonl",
    )
    service = ResultsService(
        databases=databases,
        handoffs=FakeHandoffs(handoff),
        connections=connections,
        events=events,
    )

    sources = await service.sources(project_id)
    team_src = next(s for s in sources if s[0] == "file:conn_team:checks/*.jsonl")
    assert team_src[1] == "file"
    assert team_src[2] is True

    loc_src = next(s for s in sources if s[0] == "file:conn_loc:results/*.jsonl")
    assert loc_src[1] == "file"
    assert loc_src[2] is False


@pytest.mark.asyncio
async def test_parser_kind_refuses_made_up_source_id_and_lists_real_ids(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    connections = FakeConnections()
    connections.add_local_folder("conn_team", data_dir)
    connections.add_local_folder("conn_loc", data_dir)

    handoff = _build_handoff_with_checks_and_loc(
        team_conn="conn_team",
        team_pattern="checks/*.jsonl",
        loc_conn="conn_loc",
        loc_pattern="results/*.jsonl",
    )
    service = ResultsService(
        databases=databases,
        handoffs=FakeHandoffs(handoff),
        connections=connections,
        events=events,
    )
    parsers = SqliteParsers(databases)
    kind = ParserKind(parsers, results=service)

    payload = _make_payload("file:conn_team:made_up_pattern.jsonl")
    refusals = await kind.check(project_id, payload)
    assert len(refusals) == 1
    assert (
        refusals[0]
        == "Propose the parser for one of these source ids, exactly as written: file:conn_team:checks/*.jsonl"
    )


@pytest.mark.asyncio
async def test_parser_kind_accepts_real_source_id(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    connections = FakeConnections()
    connections.add_local_folder("conn_team", data_dir)
    connections.add_local_folder("conn_loc", data_dir)

    handoff = _build_handoff_with_checks_and_loc(
        team_conn="conn_team",
        team_pattern="checks/*.jsonl",
        loc_conn="conn_loc",
        loc_pattern="results/*.jsonl",
    )
    service = ResultsService(
        databases=databases,
        handoffs=FakeHandoffs(handoff),
        connections=connections,
        events=events,
    )
    parsers = SqliteParsers(databases)
    kind = ParserKind(parsers, results=service)

    payload = _make_payload("file:conn_team:checks/*.jsonl")
    refusals = await kind.check(project_id, payload)
    assert refusals == []


@pytest.mark.asyncio
async def test_parser_kind_refuses_results_location_id(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    connections = FakeConnections()
    connections.add_local_folder("conn_team", data_dir)
    connections.add_local_folder("conn_loc", data_dir)

    handoff = _build_handoff_with_checks_and_loc(
        team_conn="conn_team",
        team_pattern="checks/*.jsonl",
        loc_conn="conn_loc",
        loc_pattern="results/*.jsonl",
    )
    service = ResultsService(
        databases=databases,
        handoffs=FakeHandoffs(handoff),
        connections=connections,
        events=events,
    )
    parsers = SqliteParsers(databases)
    kind = ParserKind(parsers, results=service)

    # results location is read without a parser, so proposing a parser for it must be refused
    payload = _make_payload("file:conn_loc:results/*.jsonl")
    refusals = await kind.check(project_id, payload)
    assert len(refusals) == 1
    assert (
        refusals[0]
        == "Propose the parser for one of these source ids, exactly as written: file:conn_team:checks/*.jsonl"
    )


@pytest.mark.asyncio
async def test_parser_kind_refuses_when_project_has_no_source_needing_parser(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    connections = FakeConnections()
    connections.add_local_folder("conn_loc", data_dir)

    # Handoff with only results_location and no team checks or feeds
    ops = Operations(
        scheduler=None,
        feeds=[],
        on_call=[],
        checks=[],
        results_location=FileResults(connection="conn_loc", path_pattern="results/*.jsonl"),
    )
    handoff = HandoffView(
        proposal_id="prop_h_none",
        version=1,
        approved=datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC),
        payload=HandoffPayload.model_validate(sample_handoff_dict(operations=ops.model_dump())),
    )
    service = ResultsService(
        databases=databases,
        handoffs=FakeHandoffs(handoff),
        connections=connections,
        events=events,
    )
    parsers = SqliteParsers(databases)
    kind = ParserKind(parsers, results=service)

    payload = _make_payload("file:conn_loc:results/*.jsonl")
    refusals = await kind.check(project_id, payload)
    assert len(refusals) == 1
    assert refusals[0] == "This project has no source that needs a parser."
