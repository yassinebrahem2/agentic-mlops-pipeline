"""Tests for parsers (mapping and code), ParserKind, and ResultsService integration (Issue #462)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from mlops_agents.connections.contract import Mount
from mlops_agents.connections.kinds import DATA_MOUNT_ROOT
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.expr import Compare
from mlops_agents.monitoring.incidents.contract import SignalDraft, SignalOutcome
from mlops_agents.monitoring.results.code_parser import CodeParserRunner
from mlops_agents.monitoring.results.contract import (
    ParserCode,
    ParserMapping,
    ParserPayload,
    ParserSpec,
)
from mlops_agents.monitoring.results.mapping import evaluate_mapping
from mlops_agents.monitoring.results.parser_kind import ParserKind
from mlops_agents.monitoring.results.parsers import SqliteParsers
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.monitoring.settings import MonitoringSettings, set_monitoring_settings
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import (
    Cadence,
    Feed,
    FileResults,
    HandoffPayload,
    JobFeedRef,
    Operations,
)
from mlops_agents.sandbox.contract import EnvironmentStatus
from mlops_agents.sandbox.docker import DockerResult, DockerTimeoutError
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import results
from tests.monitoring.results.conftest import FakeConnections, FakeHandoffs, FakeObserver
from tests.proposals.samples import handoff as sample_handoff_dict
from tests.providers.fake_clock import FakeClock
from tests.sandbox.fakes import FakeDockerRunner


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


class FakeSandbox:
    def __init__(self, tag: str = "img:test", ready: bool = True) -> None:
        self.tag = tag
        self.ready = ready

    async def rebuild(self, project_id: str) -> EnvironmentStatus:
        return EnvironmentStatus(
            state="ready" if self.ready else "failed",
            tag=self.tag if self.ready else None,
            detail=None if self.ready else "Sandbox build failed",
        )


def _build_feed_handoff(
    conn_name: str,
    pattern: str,
) -> HandoffView:
    feed = Feed(
        name="feed_test",
        kind="job",
        ref=JobFeedRef(
            file=FileResults(connection=conn_name, path_pattern=pattern),
            ci_workflow=None,
        ),
        cadence=Cadence(cron="0 0 * * *", every_minutes=None),
        grace_minutes=30,
        timezone="UTC",
    )
    ops = Operations(
        scheduler=None,
        feeds=[feed],
        on_call=[],
        checks=[],
        results_location=FileResults(connection=conn_name, path_pattern="standard/*.jsonl"),
    )
    payload = HandoffPayload.model_validate(sample_handoff_dict(operations=ops.model_dump()))
    return HandoffView(
        proposal_id="prop_h1",
        version=1,
        approved=datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC),
        payload=payload,
    )


# 1. Mapping parser tests
def test_mapping_parser_jsonl() -> None:
    mapping = ParserMapping(
        format="jsonl",
        fields={
            "check_id": "test.id",
            "ran_at": "timestamp",
            "value": "metrics.loss",
            "threshold": "metrics.thresh",
            "subject": "component",
            "category": "category",
        },
        fired_when=Compare(op="gt", field="value", value=0.5),
        defaults={"detail": {"custom": "jsonl_detail"}},
    )
    raw = (
        '{"test": {"id": "chk_1"}, "timestamp": "2026-10-30T10:00:00Z", "metrics": {"loss": 0.6, "thresh": 0.5}, "component": "model_x", "category": "model_performance"}\n'
        '{"test": {"id": "chk_2"}, "timestamp": "2026-10-30T10:05:00Z", "metrics": {"loss": 0.2, "thresh": 0.5}, "component": "model_x", "category": "model_performance"}\n'
    )
    outcome = evaluate_mapping(mapping, raw, origin_ref="data/output.jsonl")
    assert len(outcome.errors) == 0
    assert len(outcome.records) == 2

    _k1, r1 = outcome.records[0]
    assert r1.origin == "team"
    assert r1.check_id == "chk_1"
    assert r1.fired is True
    assert r1.value == 0.6
    assert r1.threshold == 0.5
    assert r1.source_ref == "data/output.jsonl"
    assert r1.detail == {"custom": "jsonl_detail"}

    _k2, r2 = outcome.records[1]
    assert r2.origin == "team"
    assert r2.check_id == "chk_2"
    assert r2.fired is False


def test_mapping_parser_json_array() -> None:
    mapping = ParserMapping(
        format="json",
        fields={
            "check_id": "id",
            "ran_at": "time",
            "value": "score",
            "threshold": "limit",
            "subject": "sub",
            "category": "cat",
        },
        fired_when=Compare(op="lt", field="value", value=0.9),
    )
    raw = json.dumps(
        [
            {
                "id": "chk_acc",
                "time": "2026-10-30T11:00:00Z",
                "score": 0.95,
                "limit": 0.9,
                "sub": "dataset",
                "cat": "data_quality",
            },
            {
                "id": "chk_acc",
                "time": "2026-10-30T11:05:00Z",
                "score": 0.85,
                "limit": 0.9,
                "sub": "dataset",
                "cat": "data_quality",
            },
        ]
    )
    outcome = evaluate_mapping(mapping, raw, origin_ref="reports/eval.json")
    assert len(outcome.errors) == 0
    assert len(outcome.records) == 2
    assert outcome.records[0][1].fired is False
    assert outcome.records[1][1].fired is True
    assert outcome.records[0][1].origin == "team"


def test_mapping_parser_csv() -> None:
    mapping = ParserMapping(
        format="csv",
        fields={
            "check_id": "test_name",
            "ran_at": "executed_at",
            "value": "null_pct",
            "threshold": "allowed_pct",
            "subject": "table_name",
            "category": "category",
        },
        fired_when=Compare(op="gt", field="value", value=0.05),
    )
    raw = (
        "test_name,executed_at,null_pct,allowed_pct,table_name,category\n"
        "null_check,2026-10-30T12:00:00Z,0.01,0.05,users,data_quality\n"
        "null_check,2026-10-30T12:10:00Z,0.08,0.05,users,data_quality\n"
    )
    outcome = evaluate_mapping(mapping, raw, origin_ref="checks/results.csv")
    assert len(outcome.errors) == 0
    assert len(outcome.records) == 2
    assert outcome.records[0][1].fired is False
    assert outcome.records[1][1].fired is True
    assert outcome.records[0][1].value == 0.01
    assert outcome.records[0][1].origin == "team"


def test_mapping_parser_log() -> None:
    mapping = ParserMapping(
        format="log",
        pattern=r"^(?P<time>\S+) \[(?P<level>\w+)\] (?P<check>\w+): value=(?P<val>[\d\.]+) threshold=(?P<thresh>[\d\.]+)$",
        fields={
            "check_id": "check",
            "ran_at": "time",
            "value": "val",
            "threshold": "thresh",
        },
        fired_when=Compare(op="gt", field="value", value=10.0),
        defaults={
            "category": "operational",
            "subject": "service_job",
        },
    )
    raw = (
        "2026-10-30T13:00:00Z [INFO] latency_check: value=5.2 threshold=10.0\n"
        "2026-10-30T13:05:00Z [WARN] latency_check: value=12.8 threshold=10.0\n"
    )
    outcome = evaluate_mapping(mapping, raw, origin_ref="var/log/app.log")
    assert len(outcome.errors) == 0
    assert len(outcome.records) == 2
    assert outcome.records[0][1].fired is False
    assert outcome.records[1][1].fired is True
    assert outcome.records[0][1].origin == "team"
    assert outcome.records[0][1].subject == "service_job"


# 2. SqliteParsers and ParserKind tests
async def test_sqlite_parsers_store_current_and_superseding(
    databases: Databases,
    project_id: str,
) -> None:
    parsers = SqliteParsers(databases)
    spec1 = ParserSpec(
        source_id="file:conn_a:logs/*.log",
        mapping=ParserMapping(
            format="log",
            fields={"check_id": "check", "ran_at": "time"},
            fired_when=Compare(op="eq", field="fired", value=True),
            defaults={"category": "operational", "subject": "job"},
        ),
    )

    v1 = await parsers.store(project_id, spec1, proposal_id="prop_p1")
    assert v1.version == 1
    assert v1.enabled is True
    assert v1.consecutive_failures == 0
    assert v1.proposal_id == "prop_p1"

    cur = await parsers.current(project_id, spec1.source_id)
    assert cur is not None
    assert cur.version == 1

    # Idempotent store with same proposal_id returns existing version 1
    v1_again = await parsers.store(project_id, spec1, proposal_id="prop_p1")
    assert v1_again.version == 1

    # New proposal supersedes
    spec2 = ParserSpec(
        source_id="file:conn_a:logs/*.log",
        mapping=ParserMapping(
            format="log",
            fields={"check_id": "check2", "ran_at": "time"},
            fired_when=Compare(op="eq", field="fired", value=True),
            defaults={"category": "operational", "subject": "job"},
        ),
    )
    v2 = await parsers.store(project_id, spec2, proposal_id="prop_p2")
    assert v2.version == 2
    cur2 = await parsers.current(project_id, spec1.source_id)
    assert cur2 is not None
    assert cur2.version == 2


async def test_parser_kind_apply_is_idempotent(
    databases: Databases,
    project_id: str,
) -> None:
    parsers = SqliteParsers(databases)
    kind = ParserKind(parsers)

    spec = ParserSpec(
        source_id="file:conn_a:output.csv",
        mapping=ParserMapping(
            format="csv",
            fields={"check_id": "c", "ran_at": "t"},
            fired_when=Compare(op="eq", field="fired", value=True),
            defaults={"category": "data_quality", "subject": "sub"},
        ),
    )
    payload = ParserPayload(
        spec=spec, sample_input="c,t\n1,2026-10-30T00:00:00Z\n", sample_records=[]
    )

    res1 = await kind.apply(project_id, "prop_apply_1", payload)
    assert res1.applied == [f"parser:{spec.source_id}"]
    assert res1.failed == {}

    # Second apply is idempotent
    res2 = await kind.apply(project_id, "prop_apply_1", payload)
    assert res2.applied == [f"parser:{spec.source_id}"]
    assert res2.failed == {}

    ver = await parsers.current(project_id, spec.source_id)
    assert ver is not None
    assert ver.version == 1


# 3. ResultsService integration tests: invalid records, raising parser, disable after failures, new version
async def test_invalid_record_skipped_with_parser_failure_signal(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "feed.jsonl"
    # Line 1 valid, Line 2 invalid (bad date), Line 3 valid
    content = (
        '{"id": "c1", "time": "2026-10-30T10:00:00Z", "val": 1.0}\n'
        '{"id": "c2", "time": "bad-date-string", "val": 2.0}\n'
        '{"id": "c3", "time": "2026-10-30T10:10:00Z", "val": 3.0}\n'
    )
    target.write_text(content, encoding="utf-8")

    connections = FakeConnections()
    connections.add_local_folder("conn_feed", data_dir)

    handoff = _build_feed_handoff("conn_feed", "feed.jsonl")
    handoffs = FakeHandoffs(handoff)

    parsers = SqliteParsers(databases)
    spec = ParserSpec(
        source_id="file:conn_feed:feed.jsonl",
        mapping=ParserMapping(
            format="jsonl",
            fields={"check_id": "id", "ran_at": "time", "value": "val"},
            fired_when=Compare(op="gt", field="value", value=5.0),
            defaults={"category": "data_quality", "subject": "tbl"},
        ),
    )
    await parsers.store(project_id, spec, proposal_id="prop_feed_1")

    incidents = FakeIncidents()
    service = ResultsService(
        databases,
        handoffs,
        connections,
        events,
        parsers=parsers,
        incidents=incidents,
    )
    observer = FakeObserver()
    service.add_observer(observer)

    now = clock.now()
    rep = await service.collect(project_id, now)

    # 2 valid records stored, invalid one skipped
    assert rep.stored == 2
    assert len(observer.batches) == 1
    assert len(observer.batches[0]) == 2
    assert [r.check_id for r in observer.batches[0]] == ["c1", "c3"]

    # parser_failure signal was raised for the invalid line
    assert len(incidents.signals) == 1
    _p_id, sig, _seen = incidents.signals[0]
    assert sig.kind == "parser_failure"
    assert sig.category == "operational"
    assert sig.subject == "file:conn_feed:feed.jsonl"


async def test_raising_parser_raises_signal_and_keeps_cursor(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "feed.jsonl"
    target.write_text('{"val": 1}\n', encoding="utf-8")

    connections = FakeConnections()
    connections.add_local_folder("conn_feed", data_dir)

    handoff = _build_feed_handoff("conn_feed", "feed.jsonl")
    handoffs = FakeHandoffs(handoff)

    parsers = SqliteParsers(databases)
    # A mapping with unknown format or raising condition
    spec = ParserSpec(
        source_id="file:conn_feed:feed.jsonl",
        mapping=ParserMapping(
            format="jsonl",
            fields={"check_id": "val", "ran_at": "nonexistent"},
            fired_when=Compare(op="gt", field="val", value=0.0),
            defaults={"category": "data_quality", "subject": "tbl"},
        ),
    )
    await parsers.store(project_id, spec, proposal_id="prop_feed_broken")

    # Override parse to simulate a parser raising an unexpected exception
    class RaisingParsers(SqliteParsers):
        async def parse(
            self,
            project_id: str,
            source_id: str,
            raw: str,
            origin_ref: str,
            *,
            start_offset: int = 0,
            csv_header: list[str] | None = None,
        ) -> Any:
            raise RuntimeError("Parser execution crashed")

    raising_parsers = RaisingParsers(databases)

    incidents = FakeIncidents()
    service = ResultsService(
        databases,
        handoffs,
        connections,
        events,
        parsers=raising_parsers,
        incidents=incidents,
    )

    now = clock.now()
    rep = await service.collect(project_id, now)
    assert rep.stored == 0

    # parser_failure signal raised
    assert len(incidents.signals) == 1
    assert incidents.signals[0][1].kind == "parser_failure"

    # Cursor is KEPT (file not consumed): cursor offset is 0
    st = await service.store.get_source_state(project_id, "file:conn_feed:feed.jsonl")
    assert st is not None
    files_cur = st.cursor.get("files", {})
    assert files_cur.get("feed.jsonl", {}).get("offset", 0) == 0


async def test_disabled_after_n_failures_and_read_again_after_new_version(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "feed.jsonl"
    target.write_text('{"id": "chk_ok", "time": "2026-10-30T10:00:00Z"}\n', encoding="utf-8")

    connections = FakeConnections()
    connections.add_local_folder("conn_feed", data_dir)

    handoff = _build_feed_handoff("conn_feed", "feed.jsonl")
    handoffs = FakeHandoffs(handoff)

    # Set parser_disable_after = 2
    await set_monitoring_settings(databases, project_id, MonitoringSettings(parser_disable_after=2))

    class CrashingParsers(SqliteParsers):
        def __init__(self, db: Databases) -> None:
            super().__init__(db)
            self.should_crash = True

        async def parse(
            self,
            project_id: str,
            source_id: str,
            raw: str,
            origin_ref: str,
            *,
            start_offset: int = 0,
            csv_header: list[str] | None = None,
        ) -> Any:
            if self.should_crash:
                raise RuntimeError("Crash on parse")
            return await super().parse(
                project_id,
                source_id,
                raw,
                origin_ref,
                start_offset=start_offset,
                csv_header=csv_header,
            )

    parsers = CrashingParsers(databases)
    spec1 = ParserSpec(
        source_id="file:conn_feed:feed.jsonl",
        mapping=ParserMapping(
            format="jsonl",
            fields={"check_id": "id", "ran_at": "time"},
            fired_when=Compare(op="eq", field="fired", value=True),
            defaults={"category": "data_quality", "subject": "tbl"},
        ),
    )
    v1 = await parsers.store(project_id, spec1, proposal_id="prop_v1")
    assert v1.enabled is True

    incidents = FakeIncidents()
    service = ResultsService(
        databases,
        handoffs,
        connections,
        events,
        parsers=parsers,
        incidents=incidents,
    )

    # Failure 1
    await service.collect(project_id, clock.now())
    cur_v1 = await parsers.current(project_id, spec1.source_id)
    assert cur_v1 is not None
    assert cur_v1.enabled is True
    assert cur_v1.consecutive_failures == 1

    # Failure 2 -> should disable parser version and emit parser.disabled event
    await service.collect(project_id, clock.now())
    cur_v1 = await parsers.current(project_id, spec1.source_id)
    assert cur_v1 is not None
    assert cur_v1.enabled is False
    assert cur_v1.consecutive_failures == 2

    # Check parser.disabled event
    all_events = await events.page(project_id)
    disabled_evts = [e for e in all_events if e.type == "parser.disabled"]
    assert len(disabled_evts) == 1
    assert disabled_evts[0].payload.get("source_id") == spec1.source_id

    # Tick 3: parser is disabled, source waits for new version (no parse attempted)
    rep3 = await service.collect(project_id, clock.now())
    assert rep3.stored == 0

    # Apply new working version
    parsers.should_crash = False
    spec2 = ParserSpec(
        source_id="file:conn_feed:feed.jsonl",
        mapping=ParserMapping(
            format="jsonl",
            fields={"check_id": "id", "ran_at": "time"},
            fired_when=Compare(op="eq", field="fired", value=True),
            defaults={"category": "data_quality", "subject": "tbl"},
        ),
    )
    v2 = await parsers.store(project_id, spec2, proposal_id="prop_v2")
    assert v2.version == 2
    assert v2.enabled is True

    # Tick 4: reads again from the cursor, stores record
    rep4 = await service.collect(project_id, clock.now())
    assert rep4.stored == 1


# 4. Code parser test
async def test_code_parser_runs_through_docker_runner(
    databases: Databases,
    project_id: str,
    tmp_path: Path,
) -> None:
    # Prepare fake mounts and docker runner
    mount_folder = tmp_path / "conn_mount"
    mount_folder.mkdir()
    conn_mount = Mount(host_path=mount_folder, container_path="/workspace/data")

    class MountsConnections(FakeConnections):
        async def mounts(self, project_id: str) -> list[Mount]:
            return [conn_mount]

    connections = MountsConnections()
    sandbox = FakeSandbox(tag="sandbox:test-tag")

    # Output from docker runner: JSON lines of parsed results
    simulated_stdout = '{"check_id": "code_chk_1", "origin": "team", "ran_at": "2026-10-30T10:00:00Z", "fired": false, "category": "operational", "subject": "job_1", "detail": {}, "source_ref": "ref_1"}\n'

    class CustomOutputDockerRunner(FakeDockerRunner):
        async def run(
            self,
            args: Any,
            *,
            timeout_s: float | None = None,
            secrets: Any = (),
        ) -> DockerResult:
            res = await super().run(args, timeout_s=timeout_s, secrets=secrets)
            if list(args) and args[0] == "run":
                return DockerResult(stdout=simulated_stdout, stderr="", exit_code=0)
            return res

    docker_runner = CustomOutputDockerRunner()

    runner = CodeParserRunner(
        sandbox=sandbox,
        docker_runner=docker_runner,
        connections=connections,
    )

    code = ParserCode(
        script="print('parsed')",
        timeout_s=15,
    )

    raw_input = "raw data to parse"
    outcome = await runner.run(
        project_id=project_id,
        code=code,
        raw=raw_input,
        origin_ref="data/test.raw",
        connection_name="conn_mount",
    )

    assert len(outcome.errors) == 0
    assert len(outcome.records) == 1
    _key, rec = outcome.records[0]
    assert rec.check_id == "code_chk_1"
    assert rec.origin == "team"

    # Check docker command args:
    # 1. Network is False: contains '--network none'
    # 2. Read-only mounts: contains '--mount' with readonly
    # 3. No env file
    assert len(docker_runner.calls) > 0
    last_args = docker_runner.calls[-1]
    assert "--network" in last_args
    assert "none" in last_args
    assert "--env-file" not in last_args
    mount_args = [a for a in last_args if "readonly" in a]
    assert len(mount_args) > 0


async def test_file_appends_for_all_formats(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    data_dir = tmp_path / "appends_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    connections = FakeConnections()
    connections.add_local_folder("conn_app", data_dir)

    # 1. JSONL Append
    jsonl_file = data_dir / "feed.jsonl"
    jsonl_file.write_text('{"id": "j1", "time": "2026-10-30T10:00:00Z"}\n', encoding="utf-8")

    # 2. CSV Append
    csv_file = data_dir / "feed.csv"
    csv_file.write_text("chk_col,time_col\nc1,2026-10-30T10:00:00Z\n", encoding="utf-8")

    # 3. JSON Array Rewrite/Append
    json_file = data_dir / "feed.json"
    json_file.write_text(
        json.dumps([{"id": "item1", "time": "2026-10-30T10:00:00Z"}]), encoding="utf-8"
    )

    # 4. Log Append
    log_file = data_dir / "feed.log"
    log_file.write_text("2026-10-30T10:00:00Z [CHECK:l1] value=1.0\n", encoding="utf-8")

    parsers = SqliteParsers(databases)

    # Store parsers for each
    await parsers.store(
        project_id,
        ParserSpec(
            source_id="file:conn_app:feed.jsonl",
            mapping=ParserMapping(
                format="jsonl",
                fields={"check_id": "id", "ran_at": "time"},
                fired_when=Compare(op="eq", field="fired", value=True),
                defaults={"category": "data_quality", "subject": "tbl"},
            ),
        ),
        proposal_id="p_jsonl",
    )
    await parsers.store(
        project_id,
        ParserSpec(
            source_id="file:conn_app:feed.csv",
            mapping=ParserMapping(
                format="csv",
                fields={"check_id": "chk_col", "ran_at": "time_col"},
                fired_when=Compare(op="eq", field="fired", value=True),
                defaults={"category": "data_quality", "subject": "tbl"},
            ),
        ),
        proposal_id="p_csv",
    )
    await parsers.store(
        project_id,
        ParserSpec(
            source_id="file:conn_app:feed.json",
            mapping=ParserMapping(
                format="json",
                fields={"check_id": "id", "ran_at": "time"},
                fired_when=Compare(op="eq", field="fired", value=True),
                defaults={"category": "data_quality", "subject": "tbl"},
            ),
        ),
        proposal_id="p_json",
    )
    await parsers.store(
        project_id,
        ParserSpec(
            source_id="file:conn_app:feed.log",
            mapping=ParserMapping(
                format="log",
                pattern=r"^(?P<time>\S+) \[CHECK:(?P<id>\w+)\]",
                fields={"check_id": "id", "ran_at": "time"},
                fired_when=Compare(op="eq", field="fired", value=True),
                defaults={"category": "data_quality", "subject": "tbl"},
            ),
        ),
        proposal_id="p_log",
    )

    feeds = [
        _build_feed_handoff("conn_app", "feed.jsonl").payload.operations.feeds[0],
        _build_feed_handoff("conn_app", "feed.csv").payload.operations.feeds[0],
        _build_feed_handoff("conn_app", "feed.json").payload.operations.feeds[0],
        _build_feed_handoff("conn_app", "feed.log").payload.operations.feeds[0],
    ]
    ops = Operations(
        scheduler=None,
        feeds=feeds,
        on_call=[],
        checks=[],
        results_location=FileResults(connection="conn_app", path_pattern="standard/*.jsonl"),
    )
    handoff = HandoffView(
        proposal_id="prop_h1",
        version=1,
        approved=datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC),
        payload=HandoffPayload.model_validate(sample_handoff_dict(operations=ops.model_dump())),
    )
    handoffs = FakeHandoffs(handoff)
    service = ResultsService(
        databases,
        handoffs,
        connections,
        events,
        parsers=parsers,
    )

    # First cycle: 1 record per format = 4 records stored
    rep1 = await service.collect(project_id, clock.now())
    assert rep1.stored == 4

    # Now append to files!
    with jsonl_file.open("a", encoding="utf-8") as f:
        f.write('{"id": "j2", "time": "2026-10-30T10:05:00Z"}\n')

    with csv_file.open("a", encoding="utf-8") as f:
        # Note: no header line appended!
        f.write("c2,2026-10-30T10:05:00Z\n")

    # JSON is re-written with one additional item
    json_file.write_text(
        json.dumps(
            [
                {"id": "item1", "time": "2026-10-30T10:00:00Z"},
                {"id": "item2", "time": "2026-10-30T10:05:00Z"},
            ]
        ),
        encoding="utf-8",
    )

    with log_file.open("a", encoding="utf-8") as f:
        f.write("2026-10-30T10:05:00Z [CHECK:l2] value=2.0\n")

    # Second cycle: each format appends 1 record = 4 new records stored!
    rep2 = await service.collect(project_id, clock.now())
    assert rep2.stored == 4

    # Verify query returns all 8 records with distinct source keys
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        all_res = (await conn.execute(select(results))).mappings().all()
    assert len(all_res) == 8
    keys = {item["source_key"] for item in all_res}
    assert len(keys) == 8


async def test_code_parser_timeout_removes_container(project_id: str, tmp_path: Path) -> None:
    class FakeTimeoutDockerRunner(FakeDockerRunner):
        def __init__(self) -> None:
            super().__init__()
            self.rm_calls: list[list[str]] = []

        async def run(
            self,
            args: Any,
            *,
            timeout_s: float | None = None,
            secrets: Any = (),
        ) -> DockerResult:
            arg_list = list(args)
            if arg_list and arg_list[0] == "run":
                raise DockerTimeoutError("timed out")
            if arg_list and arg_list[0] == "rm":
                self.rm_calls.append(arg_list)
                return DockerResult(stdout="", stderr="", exit_code=0)
            return await super().run(args, timeout_s=timeout_s, secrets=secrets)

    runner_mock = FakeTimeoutDockerRunner()
    sandbox = FakeSandbox(tag="sandbox:test-tag")

    class MountsConnections(FakeConnections):
        async def mounts(self, project_id: str) -> list[Mount]:
            return [Mount(host_path=tmp_path, container_path="/data")]

    connections = MountsConnections()

    runner = CodeParserRunner(sandbox=sandbox, docker_runner=runner_mock, connections=connections)
    code = ParserCode(script="import time; time.sleep(10)", timeout_s=1)

    with pytest.raises(RuntimeError, match="Code parser execution timed out"):
        await runner.run(project_id, code, raw="", origin_ref="test.txt", connection_name="my_conn")

    # Assert container rm -f was called!
    assert len(runner_mock.rm_calls) == 1
    rm_call = runner_mock.rm_calls[0]
    assert rm_call[0] == "rm"
    assert rm_call[1] == "-f"
    assert "parser-" in rm_call[2]


async def test_code_parser_cancelled_removes_container(project_id: str, tmp_path: Path) -> None:
    class FakeCancelledDockerRunner(FakeDockerRunner):
        def __init__(self) -> None:
            super().__init__()
            self.rm_calls: list[list[str]] = []

        async def run(
            self,
            args: Any,
            *,
            timeout_s: float | None = None,
            secrets: Any = (),
        ) -> DockerResult:
            arg_list = list(args)
            if arg_list and arg_list[0] == "run":
                raise asyncio.CancelledError()
            if arg_list and arg_list[0] == "rm":
                self.rm_calls.append(arg_list)
                return DockerResult(stdout="", stderr="", exit_code=0)
            return await super().run(args, timeout_s=timeout_s, secrets=secrets)

    runner_mock = FakeCancelledDockerRunner()
    sandbox = FakeSandbox(tag="sandbox:test-tag")

    class MountsConnections(FakeConnections):
        async def mounts(self, project_id: str) -> list[Mount]:
            return [Mount(host_path=tmp_path, container_path="/data")]

    connections = MountsConnections()

    runner = CodeParserRunner(sandbox=sandbox, docker_runner=runner_mock, connections=connections)
    code = ParserCode(script="pass", timeout_s=1)

    with pytest.raises(asyncio.CancelledError):
        await runner.run(project_id, code, raw="", origin_ref="test.txt", connection_name="my_conn")

    assert len(runner_mock.rm_calls) == 1
    rm_call = runner_mock.rm_calls[0]
    assert rm_call[0] == "rm"
    assert rm_call[1] == "-f"
    assert "parser-" in rm_call[2]


async def test_code_parser_truncation_raises_runtime_error(project_id: str, tmp_path: Path) -> None:
    class FakeTruncatedDockerRunner(FakeDockerRunner):
        async def run(
            self,
            args: Any,
            *,
            timeout_s: float | None = None,
            secrets: Any = (),
        ) -> DockerResult:
            return DockerResult(stdout="partial output", stderr="", exit_code=0, truncated=True)

    runner_mock = FakeTruncatedDockerRunner()
    sandbox = FakeSandbox(tag="sandbox:test-tag")

    class MountsConnections(FakeConnections):
        async def mounts(self, project_id: str) -> list[Mount]:
            return [Mount(host_path=tmp_path, container_path="/data")]

    connections = MountsConnections()

    runner = CodeParserRunner(sandbox=sandbox, docker_runner=runner_mock, connections=connections)
    code = ParserCode(script="pass", timeout_s=1)

    with pytest.raises(RuntimeError, match="Code parser output was truncated"):
        await runner.run(project_id, code, raw="", origin_ref="test.txt", connection_name="my_conn")


async def test_code_parser_failure_truncated_output_error_message(
    project_id: str,
    tmp_path: Path,
) -> None:
    class FailingDockerRunner:
        async def run(
            self,
            args: Any,
            *,
            timeout_s: float | None = None,
            secrets: Any = (),
        ) -> DockerResult:
            return DockerResult(
                stdout="",
                stderr="",
                exit_code=1,
                output="my error",
                truncated=True,
            )

    sandbox = FakeSandbox(tag="sandbox:test-tag")

    class MountsConnections(FakeConnections):
        async def mounts(self, project_id: str) -> list[Mount]:
            return [Mount(host_path=tmp_path, container_path="/data")]

    connections = MountsConnections()
    runner = CodeParserRunner(
        sandbox=sandbox,
        docker_runner=FailingDockerRunner(),  # pyright: ignore[reportArgumentType]
        connections=connections,
    )
    code = ParserCode(script="pass", timeout_s=1)

    with pytest.raises(RuntimeError) as exc_info:
        await runner.run(project_id, code, raw="", origin_ref="test.txt", connection_name="my_conn")

    err_str = str(exc_info.value)
    assert "True" not in err_str
    assert "my error" in err_str


async def test_source_derivation_preserves_needs_parser_when_merged_with_check(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "deriv_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    connections = FakeConnections()
    connections.add_local_folder("conn_deriv", data_dir)

    ops = Operations(
        scheduler=None,
        feeds=[_build_feed_handoff("conn_deriv", "shared/*.jsonl").payload.operations.feeds[0]],
        on_call=[],
        checks=[],
        results_location=FileResults(connection="conn_deriv", path_pattern="standard/*.jsonl"),
    )
    handoff = HandoffView(
        proposal_id="prop_h2",
        version=1,
        approved=datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC),
        payload=HandoffPayload.model_validate(sample_handoff_dict(operations=ops.model_dump())),
    )
    handoffs = FakeHandoffs(handoff)

    class FakeChecksStore:
        async def list_active(self, project_id: str) -> list[Any]:
            from mlops_agents.monitoring.expectations.contract import CheckView

            return [
                CheckView(
                    id="chk_shared",
                    check_id="shared_check",
                    category="data_quality",
                    cadence=Cadence(cron=None, every_minutes=60),
                    timezone="UTC",
                    # Check specifies same location but normally needs_parser=False
                    results_location=FileResults(
                        connection="conn_deriv", path_pattern="shared/*.jsonl"
                    ),
                    pr_number=50,
                    agent="agent",
                    version=None,
                    status="active",
                    registered=datetime.now(UTC),
                )
            ]

    service = ResultsService(
        databases,
        handoffs,
        connections,
        events,
    )
    service.bind_checks(FakeChecksStore())  # type: ignore[arg-type]

    sources = await service._derive_sources(project_id)  # pyright: ignore[reportPrivateUsage]
    shared_src = next(s for s in sources if s[0] == "file:conn_deriv:shared/*.jsonl")
    # Crucial: needs_parser must be True, not overwritten with False!
    assert shared_src[2] is True


async def test_code_parser_passes_mounted_container_path_not_raw_text(
    project_id: str,
    tmp_path: Path,
) -> None:
    captured_commands: list[list[str]] = []

    class CapturingDockerRunner(FakeDockerRunner):
        async def run(
            self,
            args: Any,
            *,
            timeout_s: float | None = None,
            secrets: Any = (),
        ) -> DockerResult:
            arg_list = list(args)
            captured_commands.append(arg_list)
            return DockerResult(
                stdout=json.dumps(
                    [
                        {
                            "check_id": "c1",
                            "ran_at": "2026-10-30T10:00:00Z",
                            "fired": False,
                            "category": "data_quality",
                            "subject": "tbl",
                        }
                    ]
                ),
                stderr="",
                exit_code=0,
            )

    runner_mock = CapturingDockerRunner()
    sandbox = FakeSandbox(tag="sandbox:test-tag")

    class MountsConnections(FakeConnections):
        async def mounts(self, project_id: str) -> list[Mount]:
            return [Mount(host_path=tmp_path, container_path="/data")]

    connections = MountsConnections()

    runner = CodeParserRunner(sandbox=sandbox, docker_runner=runner_mock, connections=connections)
    code = ParserCode(script="print('hi')", timeout_s=15)

    outcome = await runner.run(
        project_id,
        code,
        raw="THIS IS VERY LARGE RAW DATA",
        origin_ref="logs/test.json",
        connection_name="my_conn",
    )
    assert len(outcome.records) == 1
    assert len(captured_commands) > 0
    run_cmd = captured_commands[-1]
    # Verify the mounted path is passed, not the large raw string
    assert "/data/my_conn/logs/test.json" in run_cmd
    assert "THIS IS VERY LARGE RAW DATA" not in run_cmd


async def test_parser_backed_source_failure_signal_on_raw_read_error(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    data_dir = tmp_path / "missing_data"
    # Do not create data_dir -> read_raw will fail with missing connection folder
    connections = FakeConnections()
    connections.add_local_folder("conn_missing", data_dir)

    handoff = _build_feed_handoff("conn_missing", "feed.jsonl")
    handoffs = FakeHandoffs(handoff)

    parsers = SqliteParsers(databases)
    await parsers.store(
        project_id,
        ParserSpec(
            source_id="file:conn_missing:feed.jsonl",
            mapping=ParserMapping(
                format="jsonl",
                fields={"check_id": "id", "ran_at": "time"},
                fired_when=Compare(op="eq", field="fired", value=True),
                defaults={"category": "data_quality", "subject": "tbl"},
            ),
        ),
        proposal_id="p_miss",
    )

    incidents = FakeIncidents()
    service = ResultsService(
        databases,
        handoffs,
        connections,
        events,
        parsers=parsers,
        incidents=incidents,
    )

    rep = await service.collect(project_id, clock.now())
    assert rep.stored == 0
    assert "file:conn_missing:feed.jsonl" in rep.failures

    # Verify source_failure signal was raised!
    matching = [
        d
        for _p, d, _t in incidents.signals
        if d.kind == "source_failure" and d.subject == "file:conn_missing:feed.jsonl"
    ]
    assert len(matching) == 1
    assert "does not exist" in str(matching[0].detail.get("error"))


class LocalExecDockerRunner:
    """Runs the parser container's command on the host, so a code parser really executes.

    Container paths under a mount are translated back to the mount's host folder."""

    def __init__(self, image_tag: str, mounts: list[Mount]) -> None:
        self._image_tag = image_tag
        self._mounts = mounts
        self.calls: list[list[str]] = []

    def _to_host(self, arg: str) -> str:
        for mount in self._mounts:
            prefix = f"{mount.container_path}/"
            if arg.startswith(prefix):
                return str(mount.host_path / arg[len(prefix) :])
        return arg

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None = None,
        secrets: Iterable[str] = (),
    ) -> DockerResult:
        arg_list = list(args)
        self.calls.append(arg_list)
        command = arg_list[arg_list.index(self._image_tag) + 1 :]
        assert command[0] == "python"
        host_command = [sys.executable, *(self._to_host(a) for a in command[1:])]
        proc = await asyncio.to_thread(
            subprocess.run,
            host_command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout_s,
            check=False,
        )
        return DockerResult(stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)


# Reads only its stdin and emits one record per non-empty line, its subject the origin_ref argv.
_STDIN_LINES_SCRIPT = """
import json, sys
for line in sys.stdin.read().splitlines():
    if line.strip():
        print(json.dumps({
            "check_id": line.strip(),
            "ran_at": "2026-10-30T10:00:00Z",
            "fired": False,
            "category": "data_quality",
            "subject": sys.argv[1],
            "detail": {},
        }))
"""


async def test_code_parser_append_stores_only_the_new_record(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "feed.raw"
    target.write_bytes(b"c1\n")

    mount = Mount(host_path=data_dir, container_path=f"{DATA_MOUNT_ROOT}/conn_feed")

    class MountsConnections(FakeConnections):
        async def mounts(self, project_id: str) -> list[Mount]:
            return [mount]

    connections = MountsConnections()
    connections.add_local_folder("conn_feed", data_dir)
    docker_runner = LocalExecDockerRunner("sandbox:test-tag", [mount])
    runner = CodeParserRunner(
        sandbox=FakeSandbox(tag="sandbox:test-tag"),
        docker_runner=docker_runner,
        connections=connections,
    )
    parsers = SqliteParsers(databases, code_runner=runner)
    await parsers.store(
        project_id,
        ParserSpec(
            source_id="file:conn_feed:feed.raw",
            code=ParserCode(script=_STDIN_LINES_SCRIPT, timeout_s=60),
        ),
        proposal_id="prop_code",
    )
    service = ResultsService(
        databases,
        FakeHandoffs(_build_feed_handoff("conn_feed", "feed.raw")),
        connections,
        events,
        parsers=parsers,
    )

    rep1 = await service.collect(project_id, clock.now())
    assert rep1.failures == {}
    assert rep1.stored == 1

    with target.open("ab") as f:
        f.write(b"c2\n")

    rep2 = await service.collect(project_id, clock.now())
    assert rep2.failures == {}
    assert rep2.stored == 1

    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (await conn.execute(select(results))).mappings().all()
    assert sorted(r["check_id"] for r in rows) == ["c1", "c2"]
    assert len({r["source_key"] for r in rows}) == 2
    assert {r["subject"] for r in rows} == {"feed.raw"}

    # The sandbox rules still hold for the real command
    last_args = docker_runner.calls[-1]
    assert last_args[last_args.index("--network") + 1] == "none"
    assert "--env-file" not in last_args
    assert any(a.endswith(",readonly") for a in last_args if a.startswith("type=bind"))


async def test_bad_file_beside_good_one_counts_once_per_cycle_until_disabled(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    # The bad file sorts first, so a reset after the good file would hide its failure.
    bad_file = data_dir / "a_bad.jsonl"
    good_file = data_dir / "b_good.jsonl"
    bad_file.write_bytes(b"")
    good_file.write_bytes(b"")

    connections = FakeConnections()
    connections.add_local_folder("conn_feed", data_dir)
    await set_monitoring_settings(databases, project_id, MonitoringSettings(parser_disable_after=3))

    parsers = SqliteParsers(databases)
    source = "file:conn_feed:*.jsonl"
    await parsers.store(
        project_id,
        ParserSpec(
            source_id=source,
            mapping=ParserMapping(
                format="jsonl",
                fields={"check_id": "id", "ran_at": "time"},
                fired_when=Compare(op="eq", field="fired", value=True),
                defaults={"category": "data_quality", "subject": "tbl"},
            ),
        ),
        proposal_id="prop_counter",
    )
    service = ResultsService(
        databases,
        FakeHandoffs(_build_feed_handoff("conn_feed", "*.jsonl")),
        connections,
        events,
        parsers=parsers,
    )

    for cycle in (1, 2, 3):
        # Several bad records in one chunk of the bad file, and a new good record every cycle
        with bad_file.open("ab") as f:
            for n in range(3):
                f.write(f'{{"id": "b{cycle}_{n}", "time": "not-a-date"}}\n'.encode())
        with good_file.open("ab") as f:
            f.write(f'{{"id": "g{cycle}", "time": "2026-10-30T10:00:00Z"}}\n'.encode())

        rep = await service.collect(project_id, clock.now())
        assert rep.stored == 1

        ver = await parsers.current(project_id, source)
        assert ver is not None
        assert ver.consecutive_failures == cycle
        assert ver.enabled is (cycle < 3)

    disabled = [e for e in await events.page(project_id) if e.type == "parser.disabled"]
    assert len(disabled) == 1


def _csv_mapping() -> ParserMapping:
    return ParserMapping(
        format="csv",
        fields={"check_id": "id", "ran_at": "time", "subject": "note"},
        fired_when=Compare(op="eq", field="fired", value=True),
        defaults={"category": "data_quality"},
    )


def test_mapping_csv_quoted_newline_is_one_record() -> None:
    raw = (
        "id,note,time\n"
        'c1,"line one\nline two",2026-10-30T10:00:00Z\n'
        "c2,plain,2026-10-30T10:05:00Z\n"
    )
    outcome = evaluate_mapping(_csv_mapping(), raw, origin_ref="feed.csv")

    assert outcome.errors == []
    assert len(outcome.records) == 2
    (k1, r1), (k2, r2) = outcome.records
    assert r1.check_id == "c1"
    assert r1.subject == "line one\nline two"
    assert k1 == f"feed.csv:{raw.index('c1,')}"
    assert r2.check_id == "c2"
    assert k2 == f"feed.csv:{raw.index('c2,')}"


async def test_csv_record_cut_inside_quotes_is_read_whole_after_append(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "feed.csv"
    first = b'id,note,time\nc1,ok,2026-10-30T10:00:00Z\nc2,"first part\n'
    target.write_bytes(first)
    cut_offset = first.index(b"c2,")

    connections = FakeConnections()
    connections.add_local_folder("conn_feed", data_dir)
    parsers = SqliteParsers(databases)
    source = "file:conn_feed:feed.csv"
    await parsers.store(
        project_id,
        ParserSpec(source_id=source, mapping=_csv_mapping()),
        proposal_id="prop_csv",
    )
    service = ResultsService(
        databases,
        FakeHandoffs(_build_feed_handoff("conn_feed", "feed.csv")),
        connections,
        events,
        parsers=parsers,
    )

    rep1 = await service.collect(project_id, clock.now())
    assert rep1.failures == {}
    assert rep1.stored == 1
    state = await service.store.get_source_state(project_id, source)
    assert state is not None
    assert state.cursor["files"]["feed.csv"]["offset"] == cut_offset

    with target.open("ab") as f:
        f.write(b'second part",2026-10-30T10:05:00Z\n')

    rep2 = await service.collect(project_id, clock.now())
    assert rep2.failures == {}
    assert rep2.stored == 1

    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (await conn.execute(select(results))).mappings().all()
    by_check = {r["check_id"]: r for r in rows}
    assert sorted(by_check) == ["c1", "c2"]
    assert by_check["c2"]["subject"] == "first part\nsecond part"
    assert by_check["c2"]["source_key"] == f"feed.csv:{cut_offset}"


async def test_code_parser_without_connection_fails_before_running(project_id: str) -> None:
    docker_runner = FakeDockerRunner()
    runner = CodeParserRunner(
        sandbox=FakeSandbox(tag="sandbox:test-tag"),
        docker_runner=docker_runner,
        connections=FakeConnections(),
    )

    with pytest.raises(RuntimeError, match="no connection"):
        await runner.run(project_id, ParserCode(script="pass"), raw="x\n", origin_ref="feed.raw")
    assert docker_runner.calls == []
