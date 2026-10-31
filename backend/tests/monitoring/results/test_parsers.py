"""Tests for parsers (mapping and code), ParserKind, and ResultsService integration (Issue #462)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mlops_agents.connections.contract import Mount
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
from mlops_agents.sandbox.docker import DockerResult
from mlops_agents.storage.databases import Databases
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
        async def parse(self, project_id: str, source_id: str, raw: str, origin_ref: str) -> Any:
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

        async def parse(self, project_id: str, source_id: str, raw: str, origin_ref: str) -> Any:
            if self.should_crash:
                raise RuntimeError("Crash on parse")
            return await super().parse(project_id, source_id, raw, origin_ref)

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
