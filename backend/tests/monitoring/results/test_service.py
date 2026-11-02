"""Tests for ResultsService, collect, delivery, observers and event log (I22)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mlops_agents.api import services as services_module
from mlops_agents.api.services import Services
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.results.contract import (
    ResultRecord,
    StoredResult,
    source_id,
)
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.monitoring.scheduler.loop import AppScheduler
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import (
    Cadence,
    CheckSpec,
    Feed,
    FileResults,
    HandoffPayload,
    JobFeedRef,
    Operations,
)
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import projects
from mlops_agents.storage.tables import results as results_table
from tests.api.conftest import started_app
from tests.monitoring.results.conftest import (
    FakeConnections,
    FakeHandoffs,
    FakeObserver,
    HoldingObserver,
    InMemoryResultsStore,
)
from tests.proposals.samples import handoff as sample_handoff_dict
from tests.providers.fake_clock import FakeClock

# Only stops a broken test from hanging; no assertion depends on it.
_HANG_GUARD_S = 10.0


def _build_handoff(
    results_loc: FileResults | None = None,
    checks: list[CheckSpec] | None = None,
    feeds: list[Feed] | None = None,
) -> HandoffView:
    ops = Operations(
        scheduler=None,
        feeds=feeds or [],
        on_call=[],
        checks=checks or [],
        results_location=results_loc or FileResults(connection="conn_a", path_pattern="*.jsonl"),
    )
    payload = HandoffPayload.model_validate(sample_handoff_dict(operations=ops.model_dump()))
    return HandoffView(
        proposal_id="prop_1",
        version=1,
        approved=datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC),
        payload=payload,
    )


async def test_collecting_twice_stores_nothing_twice(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "results.jsonl"
    line = b'{"check_id": "c1", "origin": "agent", "ran_at": "2026-10-30T10:00:00Z", "fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    target.write_bytes(line)

    connections = FakeConnections()
    connections.add_local_folder("conn_a", data_dir)

    handoff = _build_handoff(results_loc=FileResults(connection="conn_a", path_pattern="*.jsonl"))
    handoffs = FakeHandoffs(handoff)

    service = ResultsService(databases, handoffs, connections, events)
    observer = FakeObserver()
    service.add_observer(observer)

    # First collect
    rep1 = await service.collect(project_id, clock.now())
    assert rep1.stored == 1
    assert len(observer.batches) == 1
    assert len(observer.batches[0]) == 1

    # Second collect -> unchanged file, 0 stored, observer not called again
    rep2 = await service.collect(project_id, clock.now())
    assert rep2.stored == 0
    assert len(observer.batches) == 1

    # Verify results.collected event was recorded
    evts = await events.page(project_id)
    assert any(e.type == "results.collected" for e in evts)


async def test_two_sources_interleaved_times_reach_observer_in_ran_at_order(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    dir_a = tmp_path / "dir_a"
    dir_b = tmp_path / "dir_b"
    dir_a.mkdir(parents=True, exist_ok=True)
    dir_b.mkdir(parents=True, exist_ok=True)

    # Source A has records at 10:00 and 12:00
    file_a = dir_a / "res.jsonl"
    file_a.write_bytes(
        b'{"check_id": "c_a1", "origin": "agent", "ran_at": "2026-10-30T10:00:00Z", "fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
        b'{"check_id": "c_a2", "origin": "agent", "ran_at": "2026-10-30T12:00:00Z", "fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    )

    # Source B has records at 09:00 and 11:00
    file_b = dir_b / "res.jsonl"
    file_b.write_bytes(
        b'{"check_id": "c_b1", "origin": "agent", "ran_at": "2026-10-30T09:00:00Z", "fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
        b'{"check_id": "c_b2", "origin": "agent", "ran_at": "2026-10-30T11:00:00Z", "fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    )

    connections = FakeConnections()
    connections.add_local_folder("conn_a", dir_a)
    connections.add_local_folder("conn_b", dir_b)

    handoff = _build_handoff(
        results_loc=FileResults(connection="conn_a", path_pattern="*.jsonl"),
        checks=[
            CheckSpec(
                check_id="c_b",
                name="Check B",
                category="data_quality",
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                timezone="UTC",
                results=FileResults(connection="conn_b", path_pattern="*.jsonl"),
            )
        ],
    )
    handoffs = FakeHandoffs(handoff)

    service = ResultsService(databases, handoffs, connections, events)
    observer = FakeObserver()
    service.add_observer(observer)

    rep = await service.collect(project_id, clock.now())
    assert rep.stored == 4

    assert len(observer.batches) == 1
    delivered = observer.batches[0]
    assert [d.check_id for d in delivered] == ["c_b1", "c_a1", "c_b2", "c_a2"]


async def test_results_committed_not_delivered_are_delivered_at_next_start_once(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    connections = FakeConnections()
    handoffs = FakeHandoffs(None)

    service = ResultsService(databases, handoffs, connections, events)
    s_id = source_id("file", "*.jsonl", "data_conn")
    t0 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)

    # Ingest without delivery (store directly leaves delivered_at as NULL)
    rec = ResultRecord(
        check_id="chk_undelivered",
        origin="agent",
        ran_at=t0,
        fired=False,
        category="data_quality",
        subject="s",
        detail={},
        source_ref="r",
    )
    await service.ingest(project_id, s_id, [("k1", rec)], seen_at=t0)

    # Verify delivered_at is NULL in store
    undelivered = await service.store.fetch_undelivered(project_id)
    assert len(undelivered) == 1

    # Calling deliver_undelivered with no observers does NOT mark records delivered
    await service.deliver_undelivered(project_id, clock.now())
    undelivered_still = await service.store.fetch_undelivered(project_id)
    assert len(undelivered_still) == 1
    assert undelivered_still[0].id == undelivered[0].id

    # Attach observer and run deliver_undelivered (startup hook)
    obs = FakeObserver()
    service.add_observer(obs)
    await service.deliver_undelivered(project_id, clock.now())

    assert len(obs.batches) == 1
    assert obs.batches[0][0].check_id == "chk_undelivered"

    # Subsequent run delivers nothing again
    await service.deliver_undelivered(project_id, clock.now())
    assert len(obs.batches) == 1


def _stored(result_id: str) -> StoredResult:
    ran_at = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
    return StoredResult(
        id=result_id,
        source_id=source_id("file", "*.jsonl", "conn_a"),
        source_key=f"{result_id}:0",
        seen_at=ran_at,
        check_id="c1",
        origin="agent",
        ran_at=ran_at,
        fired=False,
        category="data_quality",
        subject="s",
        detail={},
        source_ref="r",
    )


async def _signal_then(started: asyncio.Event, work: Awaitable[object]) -> None:
    started.set()
    await work


async def test_second_delivery_during_a_held_one_delivers_nothing_twice(
    databases: Databases, events: SqliteEventLog, clock: FakeClock
) -> None:
    store = InMemoryResultsStore(databases)
    store.undelivered["prj_a"] = [_stored("res_1")]
    service = ResultsService(databases, FakeHandoffs(None), FakeConnections(), events, store=store)
    observer = HoldingObserver("prj_a")
    service.add_observer(observer)

    first = asyncio.create_task(service.deliver_undelivered("prj_a", clock.now()))
    await observer.entered.wait()
    second_started = asyncio.Event()
    second = asyncio.create_task(
        _signal_then(second_started, service.deliver_undelivered("prj_a", clock.now()))
    )
    # Nothing in the store suspends: the second delivery has now run until it waits for the
    # first, or, unguarded, to its end.
    await second_started.wait()
    observer.release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=_HANG_GUARD_S)

    assert observer.batches == [("prj_a", ["res_1"])]
    assert store.mark_calls == [("prj_a", ["res_1"])]


async def test_delivery_during_a_held_collect_delivers_nothing_twice(
    databases: Databases, events: SqliteEventLog, tmp_path: Path, clock: FakeClock
) -> None:
    data_dir = tmp_path / "results_folder"
    data_dir.mkdir()
    (data_dir / "results.jsonl").write_bytes(
        b'{"check_id": "c1", "origin": "agent", "ran_at": "2026-10-30T10:00:00Z", "fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    )
    connections = FakeConnections()
    connections.add_local_folder("conn_a", data_dir)
    handoffs = FakeHandoffs(
        _build_handoff(results_loc=FileResults(connection="conn_a", path_pattern="*.jsonl"))
    )
    store = InMemoryResultsStore(databases)
    service = ResultsService(databases, handoffs, connections, events, store=store)
    observer = HoldingObserver("prj_a")
    service.add_observer(observer)

    # Nothing is undelivered at collect's start, so the observer's first call is collect's own
    # delivery of the rows it has just stored, still undelivered in the store.
    collecting = asyncio.create_task(service.collect("prj_a", clock.now()))
    await observer.entered.wait()
    (stored_id,) = observer.batches[0][1]
    delivery_started = asyncio.Event()
    delivery = asyncio.create_task(
        _signal_then(delivery_started, service.deliver_undelivered("prj_a", clock.now()))
    )
    await delivery_started.wait()
    observer.release.set()
    await asyncio.wait_for(asyncio.gather(collecting, delivery), timeout=_HANG_GUARD_S)

    assert observer.batches == [("prj_a", [stored_id])]
    assert store.mark_calls == [("prj_a", [stored_id])]


async def test_delivery_for_another_project_is_not_held_up(
    databases: Databases, events: SqliteEventLog, clock: FakeClock
) -> None:
    store = InMemoryResultsStore(databases)
    store.undelivered["prj_a"] = [_stored("res_a")]
    store.undelivered["prj_b"] = [_stored("res_b")]
    service = ResultsService(databases, FakeHandoffs(None), FakeConnections(), events, store=store)
    observer = HoldingObserver("prj_a")
    service.add_observer(observer)

    held = asyncio.create_task(service.deliver_undelivered("prj_a", clock.now()))
    await observer.entered.wait()

    # Completes while project A's delivery is held; the timeout only guards against a hang.
    await asyncio.wait_for(service.deliver_undelivered("prj_b", clock.now()), timeout=_HANG_GUARD_S)
    assert observer.batches == [("prj_a", ["res_a"]), ("prj_b", ["res_b"])]
    assert store.mark_calls == [("prj_b", ["res_b"])]

    observer.release.set()
    await asyncio.wait_for(held, timeout=_HANG_GUARD_S)
    assert store.mark_calls == [("prj_b", ["res_b"]), ("prj_a", ["res_a"])]


async def test_startup_delivers_a_pending_result_once_before_the_scheduler_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    paths = DataPaths(home)
    paths.ensure()

    # Pre-populate app database with a project and an undelivered result
    databases = Databases(paths)
    await databases.app()
    project_id = "prj_wiring"
    async with (await databases.app()).begin() as conn:
        await conn.execute(
            projects.insert().values(
                id=project_id,
                name="Wiring Test",
                source="local",
                local_path=str(home / "repo"),
                local_path_key="key",
                status="ok",
                created_at="2026-10-30T10:00:00Z",
            )
        )

    proj_engine = await databases.project(project_id)
    t0 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
    async with proj_engine.begin() as conn:
        await conn.execute(
            results_table.insert().values(
                id="res_pending_1",
                source_id="file:*.jsonl:data",
                source_key="results.jsonl:0",
                check_id="chk_pending",
                origin="agent",
                ran_at=t0.isoformat(),
                fired=False,
                category="data_quality",
                subject="s",
                detail="{}",
                source_ref="r",
                seen_at=t0.isoformat(),
                delivered_at=None,
            )
        )
    await databases.dispose()

    # The zero-observer case is covered by
    # test_results_committed_not_delivered_are_delivered_at_next_start_once: the real wiring
    # always registers the incidents observer.
    # A later plan's observer is registered on results while build_services wires the services.
    order: list[str] = []
    delivered_batches: list[list[StoredResult]] = []

    class RealPlanObserver:
        async def stored(self, project_id: str, results: Sequence[StoredResult]) -> None:
            order.append("delivered")
            delivered_batches.append(list(results))

    real_results_service_cls = services_module.ResultsService

    class WiringResultsService(real_results_service_cls):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.add_observer(RealPlanObserver())

    monkeypatch.setattr(services_module, "ResultsService", WiringResultsService)

    # The scheduler's first collection can only come after its start.
    real_start = AppScheduler.start

    async def recording_start(self: AppScheduler) -> None:
        order.append("scheduler.start")
        await real_start(self)

    monkeypatch.setattr(AppScheduler, "start", recording_start)

    async with started_app(home) as app:
        services: Services = app.state.services
        # The observer registered during build_services received the pending result once, at
        # startup, before the scheduler started.
        assert order == ["delivered", "scheduler.start"]
        assert len(delivered_batches) == 1
        assert len(delivered_batches[0]) == 1
        assert delivered_batches[0][0].id == "res_pending_1"

        # And now it is marked delivered in the DB
        undelivered_after = await services.results.store.fetch_undelivered(project_id)
        assert len(undelivered_after) == 0


async def test_raising_observer_does_not_stop_next_and_batch_left_undelivered(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "res.jsonl"
    target.write_bytes(
        b'{"check_id": "c1", "origin": "agent", "ran_at": "2026-10-30T10:00:00Z", "fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    )

    connections = FakeConnections()
    connections.add_local_folder("conn_a", data_dir)
    handoffs = FakeHandoffs(
        _build_handoff(results_loc=FileResults(connection="conn_a", path_pattern="*.jsonl"))
    )

    service = ResultsService(databases, handoffs, connections, events)

    # Observer 1 raises
    obs_raising = FakeObserver(name="raising", should_raise=RuntimeError("observer exploded"))
    # Observer 2 succeeds
    obs_healthy = FakeObserver(name="healthy")

    service.add_observer(obs_raising)
    service.add_observer(obs_healthy)

    # Collect should not raise
    rep = await service.collect(project_id, clock.now())
    assert rep.stored == 1

    # Healthy observer should have received the batch
    assert len(obs_healthy.batches) == 1
    assert obs_healthy.batches[0][0].check_id == "c1"

    # Because an observer raised, batch must remain undelivered for the next tick
    undelivered = await service.store.fetch_undelivered(project_id)
    assert len(undelivered) == 1
    assert undelivered[0].check_id == "c1"

    # When observer recovers, next delivery succeeds and marks delivered
    obs_raising.should_raise = None
    clock.advance(60)
    await service.deliver_undelivered(project_id, clock.now())
    assert len(await service.store.fetch_undelivered(project_id)) == 0


async def test_cursor_survives_new_service_instance_restart(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "res.jsonl"
    target.write_bytes(
        b'{"check_id": "c1", "origin": "agent", "ran_at": "2026-10-30T10:00:00Z", "fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    )

    connections = FakeConnections()
    connections.add_local_folder("conn_a", data_dir)
    handoffs = FakeHandoffs(
        _build_handoff(results_loc=FileResults(connection="conn_a", path_pattern="*.jsonl"))
    )

    # Service instance 1
    service1 = ResultsService(databases, handoffs, connections, events)
    obs1 = FakeObserver()
    service1.add_observer(obs1)
    rep1 = await service1.collect(project_id, clock.now())
    assert rep1.stored == 1
    assert len(obs1.batches) == 1

    # Service instance 2 (simulating restart)
    service2 = ResultsService(databases, handoffs, connections, events)
    obs2 = FakeObserver()
    service2.add_observer(obs2)

    rep2 = await service2.collect(project_id, clock.now())
    assert rep2.stored == 0
    assert len(obs2.batches) == 0


async def test_team_source_without_parser_cursor_stays_at_zero(
    databases: Databases,
    events: SqliteEventLog,
    project_id: str,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    connections = FakeConnections()
    connections.add_local_folder("conn_team", data_dir)

    handoff = _build_handoff(
        feeds=[
            Feed(
                name="team_job_feed",
                kind="job",
                ref=JobFeedRef(
                    ci_workflow=None,
                    file=FileResults(connection="conn_team", path_pattern="job_output.log"),
                ),
                cadence=Cadence(cron="0 0 * * *", every_minutes=None),
                grace_minutes=30,
                timezone="UTC",
            )
        ]
    )
    handoffs = FakeHandoffs(handoff)

    service = ResultsService(databases, handoffs, connections, events)
    rep = await service.collect(project_id, clock.now())

    s_id = source_id("file", "job_output.log", "conn_team")
    assert s_id in rep.by_source
    assert rep.by_source[s_id] == 0

    state = await service.store.get_source_state(project_id, s_id)
    assert state is not None
    assert state.cursor == {}
