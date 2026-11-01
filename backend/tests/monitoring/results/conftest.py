"""Shared fixtures for results tests."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert

from mlops_agents.connections.contract import (
    LOCAL_FOLDER_KIND,
    ConnectionView,
    DuckDbSource,
    Finding,
    Findings,
    Mount,
    TableRef,
)
from mlops_agents.connections.kinds import DATA_MOUNT_ROOT
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.incidents.contract import SignalDraft, SignalOutcome
from mlops_agents.monitoring.results.code_parser import CodeParserRunner
from mlops_agents.monitoring.results.contract import (
    ParserCode,
    ParserSpec,
    ResultRecord,
    ResultsObserver,
    SourceState,
    StoredResult,
)
from mlops_agents.monitoring.results.parsers import SqliteParsers
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.monitoring.results.store import ResultsStore
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.contract import HandoffObserver, HandoffView
from mlops_agents.proposals.payloads import (
    Cadence,
    CheckSpec,
    Feed,
    FileResults,
    HandoffPayload,
    JobFeedRef,
    MlflowResults,
    Operations,
)
from mlops_agents.sandbox.contract import EnvironmentStatus
from mlops_agents.sandbox.docker import DockerResult, StreamCapture
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import projects
from mlops_agents.tracking.contract import SourceType
from mlops_agents.tracking.service import TrackingService
from tests.conftest import MemoryKeyStore
from tests.proposals.samples import handoff as sample_handoff_dict
from tests.providers.fake_clock import FakeClock
from tests.tracking.conftest import FakeHandoffs as TrackingFakeHandoffs
from tests.tracking.conftest import FakeTrackingAdapter, TrackingEnv
from tests.tracking.conftest import TestClock as ClockForTest


class FakeObserver(ResultsObserver):
    def __init__(self, name: str = "fake", should_raise: Exception | None = None) -> None:
        self.name = name
        self.should_raise = should_raise
        self.batches: list[list[StoredResult]] = []

    async def stored(self, project_id: str, results: Sequence[StoredResult]) -> None:
        if self.should_raise is not None:
            raise self.should_raise
        self.batches.append(list(results))


class HoldingObserver(ResultsObserver):
    """Records every batch; holds its first call for `hold_project` until `release` is set."""

    def __init__(self, hold_project: str) -> None:
        self._hold_project = hold_project
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.batches: list[tuple[str, list[str]]] = []

    async def stored(self, project_id: str, results: Sequence[StoredResult]) -> None:
        self.batches.append((project_id, [r.id for r in results]))
        if project_id == self._hold_project and not self.entered.is_set():
            self.entered.set()
            await self.release.wait()


class InMemoryResultsStore(ResultsStore):
    """Results rows and source states in memory. No method awaits, so the only suspension
    points in a delivery are the service's own and the observer's."""

    def __init__(self, databases: Databases) -> None:
        super().__init__(databases)
        self.undelivered: dict[str, list[StoredResult]] = {}
        self.mark_calls: list[tuple[str, list[str]]] = []
        self.states: dict[tuple[str, str], SourceState] = {}

    async def fetch_undelivered(self, project_id: str) -> list[StoredResult]:
        return list(self.undelivered.get(project_id, []))

    async def mark_delivered(
        self, project_id: str, ids: Sequence[str], delivered_at: datetime
    ) -> None:
        if not ids:
            return
        self.mark_calls.append((project_id, list(ids)))
        marked = set(ids)
        rows = self.undelivered.get(project_id, [])
        self.undelivered[project_id] = [r for r in rows if r.id not in marked]

    async def get_source_state(self, project_id: str, source_id: str) -> SourceState | None:
        return self.states.get((project_id, source_id))

    async def save_source_state(self, project_id: str, state: SourceState) -> None:
        self.states[(project_id, state.source_id)] = state

    async def save_items_and_state(
        self,
        project_id: str,
        source_id: str,
        items: Sequence[tuple[str, ResultRecord]],
        seen_at: datetime,
        state: SourceState,
    ) -> list[StoredResult]:
        inserted = [
            StoredResult(
                **record.model_dump(),
                id=f"res_{source_key}",
                source_id=source_id,
                source_key=source_key,
                seen_at=seen_at,
            )
            for source_key, record in items
        ]
        self.undelivered.setdefault(project_id, []).extend(inserted)
        self.states[(project_id, source_id)] = state
        return inserted


class FakeHandoffs:
    def __init__(self, handoff: HandoffView | None = None) -> None:
        self._current: HandoffView | None = handoff

    def set_handoff(self, handoff: HandoffView | None) -> None:
        self._current = handoff

    def add_observer(self, observer: HandoffObserver) -> None:
        pass

    async def current(self, project_id: str) -> HandoffView | None:
        return self._current

    async def store(self, project_id: str, proposal_id: str, payload: HandoffPayload) -> int:
        raise NotImplementedError


class FakeConnections:
    def __init__(self, connections: list[ConnectionView] | None = None) -> None:
        self._connections: dict[str, ConnectionView] = {c.name: c for c in (connections or [])}

    def add_local_folder(
        self, name: str, folder: Path, secret_files: list[str] | None = None
    ) -> ConnectionView:
        findings = Findings(
            unsupported_files=[],
            secret_files=[Finding(path=s, reason="denylist") for s in (secret_files or [])],
            truncated=False,
        )
        view = ConnectionView(
            id=f"conn_{name}",
            name=name,
            kind=LOCAL_FOLDER_KIND,
            settings={"path": str(folder)},
            status="ok",
            status_detail=None,
            findings=findings,
            data_version="1",
        )
        self._connections[name] = view
        return view

    async def list(self, project_id: str) -> list[ConnectionView]:
        return list(self._connections.values())

    async def inspect(self, project_id: str, kind: str, settings: dict[str, Any]) -> Findings:
        # Match connection by settings path or return inspect findings
        for view in self._connections.values():
            if view.kind == kind and view.settings == settings:
                return view.findings
        return Findings(unsupported_files=[], secret_files=[], truncated=False)

    async def create(
        self, project_id: str, kind: str, name: str, settings: dict[str, Any]
    ) -> ConnectionView:
        raise NotImplementedError

    async def update(
        self, project_id: str, connection_id: str, settings: dict[str, Any]
    ) -> ConnectionView:
        raise NotImplementedError

    async def delete(self, project_id: str, connection_id: str) -> None:
        raise NotImplementedError

    async def tables(self, project_id: str, connection_id: str) -> list[TableRef]:
        raise NotImplementedError

    async def data_version(self, project_id: str, connection_id: str, table: str) -> str:
        raise NotImplementedError

    async def duckdb_source(self, project_id: str, connection_id: str, table: str) -> DuckDbSource:
        raise NotImplementedError

    async def mounts(self, project_id: str) -> list[Mount]:
        raise NotImplementedError


@pytest.fixture
def project_id() -> str:
    return "prj_testresults"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def events(databases: Databases) -> SqliteEventLog:
    return SqliteEventLog(databases)


@pytest.fixture
def fake_observer() -> FakeObserver:
    return FakeObserver()


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
        self.rebuilds = 0

    async def rebuild(self, project_id: str) -> EnvironmentStatus:
        self.rebuilds += 1
        return EnvironmentStatus(
            state="ready" if self.ready else "failed",
            tag=self.tag if self.ready else None,
            detail=None if self.ready else "Sandbox build failed",
        )


def build_feed_handoff(
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


def build_mlflow_handoff(
    checks: list[CheckSpec] | None = None,
    results_location: FileResults | MlflowResults | None = None,
) -> HandoffView:
    ops = Operations(
        scheduler=None,
        feeds=[],
        on_call=[],
        checks=checks or [],
        results_location=results_location
        or FileResults(connection="conn_a", path_pattern="*.jsonl"),
    )
    payload = HandoffPayload.model_validate(sample_handoff_dict(operations=ops.model_dump()))
    return HandoffView(
        proposal_id="prop_mlflow",
        version=1,
        approved=datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC),
        payload=payload,
    )


@pytest.fixture
async def tracking_env(
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
) -> TrackingEnv:
    project_id = "prj_mlflowtest"
    engine = await databases.app()
    async with engine.begin() as conn:
        await conn.execute(
            insert(projects).values(
                id=project_id,
                name="MLflow Test Project",
                source="github",
                github_full_name="org/demo",
                github_key="org/demo",
                default_branch="main",
                status="ok",
                created_at=datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC).isoformat(),
            )
        )
    await databases.project(project_id)
    clock = ClockForTest(datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC))
    secrets: Any = None
    events = SqliteEventLog(databases)
    adapter = FakeTrackingAdapter("mlflow")
    adapters: dict[SourceType, FakeTrackingAdapter] = {"mlflow": adapter}
    handoffs = TrackingFakeHandoffs()

    async def _async_token() -> str | None:
        return "fake-token"

    service = TrackingService(
        databases=databases,
        keystore=keystore,
        secrets=secrets,
        events=events,
        adapters=adapters,
        github_token=_async_token,
        clock=clock,
        handoffs=handoffs,
    )

    return TrackingEnv(
        service=service,
        databases=databases,
        keystore=keystore,
        secrets=secrets,
        events=events,
        adapters=adapters,
        project_id=project_id,
        clock=clock,  # type: ignore[arg-type]
        handoffs=handoffs,
    )


def mapping_feed_service(
    databases: Databases,
    events: SqliteEventLog,
    data_dir: Path,
    pattern: str,
    parsers: SqliteParsers,
    incidents: FakeIncidents | None = None,
) -> ResultsService:
    connections = FakeConnections()
    connections.add_local_folder("conn_feed", data_dir)
    return ResultsService(
        databases,
        FakeHandoffs(build_feed_handoff("conn_feed", pattern)),
        connections,
        events,
        parsers=parsers,
        incidents=incidents,
    )


class LocalExecDockerRunner:
    """Runs the parser container's command on the host, so a code parser really executes.

    Container paths under a mount are translated back to the mount's host folder. Output is
    bounded by the production capture, so a run that prints too much is truncated as it is
    in a real container."""

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
        stdout, stderr, combined = StreamCapture(), StreamCapture(), StreamCapture()
        stdout.feed(proc.stdout)
        stderr.feed(proc.stderr)
        combined.feed(proc.stdout)
        combined.feed(proc.stderr)
        return DockerResult(
            stdout=stdout.get_output(),
            stderr=stderr.get_output(),
            exit_code=proc.returncode,
            output=combined.get_output(),
            truncated=combined.truncated,
        )


# Reads only its stdin and emits one record per non-empty line, its subject the origin_ref argv.
STDIN_LINES_SCRIPT = """
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


class MountsConnections(FakeConnections):
    """Connections whose mounts are `mounts`, counting the lookups; with `mounts_error` set,
    every lookup raises it."""

    def __init__(self, mounts: list[Mount], mounts_error: str | None = None) -> None:
        super().__init__()
        self._mounts = mounts
        self._mounts_error = mounts_error
        self.mount_lookups = 0

    async def mounts(self, project_id: str) -> list[Mount]:
        self.mount_lookups += 1
        if self._mounts_error is not None:
            raise RuntimeError(self._mounts_error)
        return self._mounts


def feed_mount(data_dir: Path) -> Mount:
    """The conn_feed connection's mount of `data_dir`."""
    return Mount(host_path=data_dir, container_path=f"{DATA_MOUNT_ROOT}/conn_feed")


async def code_feed_service(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    data_dir: Path,
    filename: str,
    script: str,
    incidents: FakeIncidents | None = None,
    *,
    sandbox: FakeSandbox | None = None,
    connections: MountsConnections | None = None,
) -> tuple[ResultsService, LocalExecDockerRunner]:
    mount = feed_mount(data_dir)
    if connections is None:
        connections = MountsConnections([mount])
    connections.add_local_folder("conn_feed", data_dir)
    docker_runner = LocalExecDockerRunner("sandbox:test-tag", [mount])
    runner = CodeParserRunner(
        sandbox=sandbox or FakeSandbox(tag="sandbox:test-tag"),
        docker_runner=docker_runner,
        connections=connections,
    )
    parsers = SqliteParsers(databases, code_runner=runner)
    await parsers.store(
        project_id,
        ParserSpec(
            source_id=f"file:conn_feed:{filename}",
            code=ParserCode(script=script, timeout_s=60),
        ),
        proposal_id="prop_code",
    )
    service = ResultsService(
        databases,
        FakeHandoffs(build_feed_handoff("conn_feed", filename)),
        connections,
        events,
        parsers=parsers,
        incidents=incidents,
    )
    return service, docker_runner


def set_mtime(path: Path, when: datetime) -> None:
    """Sets the file's modification time, so a test decides how long it has been unchanged."""
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))
