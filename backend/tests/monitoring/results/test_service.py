"""Tests for ResultsService, collect, delivery, observers and event log (I22)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.results.contract import (
    ResultRecord,
    source_id,
)
from mlops_agents.monitoring.results.service import ResultsService
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
from tests.monitoring.results.conftest import FakeConnections, FakeHandoffs, FakeObserver
from tests.proposals.samples import handoff as sample_handoff_dict
from tests.providers.fake_clock import FakeClock


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

    # Attach observer and run deliver_undelivered (startup hook)
    obs = FakeObserver()
    service.add_observer(obs)
    await service.deliver_undelivered(project_id, clock.now())

    assert len(obs.batches) == 1
    assert obs.batches[0][0].check_id == "chk_undelivered"

    # Subsequent run delivers nothing again
    await service.deliver_undelivered(project_id, clock.now())
    assert len(obs.batches) == 1


async def test_raising_observer_does_not_stop_next_and_batch_marked_delivered(
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

    # Batch should be marked delivered in DB
    undelivered = await service.store.fetch_undelivered(project_id)
    assert len(undelivered) == 0


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
    rep1 = await service1.collect(project_id, clock.now())
    assert rep1.stored == 1

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
