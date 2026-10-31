"""Tests for RegisterCheck tool, SqliteChecks store, and PR activation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from mlops_agents.agents.pr_sync import PrChange
from mlops_agents.agents.tools.base import RiskClass, ToolContext
from mlops_agents.agents.tools.register_check import RegisterCheck, RegisterCheckInput
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.expectations.checks import ChecksQueryProvider, SqliteChecks
from mlops_agents.monitoring.expectations.service import ExpectationsService
from mlops_agents.monitoring.incidents.contract import SignalDraft, SignalOutcome
from mlops_agents.monitoring.query import MonitoringQuery
from mlops_agents.monitoring.results.contract import ResultRecord
from mlops_agents.monitoring.results.service import ResultsService
from mlops_agents.monitoring.settings import MonitoringSettings, set_monitoring_settings
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.payloads import Cadence, FileResults, MlflowResults
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import expectations
from tests.monitoring.results.conftest import FakeConnections, FakeHandoffs
from tests.providers.fake_clock import FakeClock


class FakeIncidents:
    def __init__(self) -> None:
        self.signals: list[tuple[str, SignalDraft, datetime]] = []

    async def raise_signal(
        self, project_id: str, draft: SignalDraft, seen_at: datetime
    ) -> SignalOutcome:
        self.signals.append((project_id, draft, seen_at))
        return SignalOutcome(
            signal_id=f"sig_{len(self.signals)}",
            incident_id=f"inc_{len(self.signals)}",
            change="opened",
            severity="medium",
        )


class FakeMaintenance:
    async def suspended(self, project_id: str, scope: Any, at: datetime) -> bool:
        return False


class DummyFileAccess:
    @property
    def root(self) -> Path:
        return Path("dummy_root")

    async def tracked(self) -> list[str]:
        return []

    async def under(self, prefix: str) -> list[str]:
        return []

    async def resolve_file(self, user_path: str) -> str:
        return user_path

    async def resolve_dir(self, user_path: str | None) -> str:
        return user_path or ""

    def invalidate(self) -> None:
        pass


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC))


@pytest.fixture
def databases(tmp_path: Path) -> Databases:
    return Databases(DataPaths(tmp_path / "data"))


@pytest.fixture
def events(databases: Databases) -> SqliteEventLog:
    return SqliteEventLog(databases)


@pytest.fixture
def fake_incidents() -> FakeIncidents:
    return FakeIncidents()


@pytest.fixture
def expectations_service(
    databases: Databases,
    fake_incidents: FakeIncidents,
    fake_clock: FakeClock,
) -> ExpectationsService:
    return ExpectationsService(
        databases=databases,
        handoffs=FakeHandoffs(),
        connections=FakeConnections(),
        incidents=fake_incidents,
        tracking=None,
        maintenance=FakeMaintenance(),
        clock=fake_clock,
    )


@pytest.fixture
def checks_store(
    databases: Databases,
    expectations_service: ExpectationsService,
    events: SqliteEventLog,
    fake_clock: FakeClock,
) -> SqliteChecks:
    return SqliteChecks(
        databases=databases,
        expectations=expectations_service,
        events=events,
        clock=fake_clock,
    )


@pytest.mark.asyncio
async def test_register_then_merge_activates_check_and_expectation(
    checks_store: SqliteChecks,
    expectations_service: ExpectationsService,
    databases: Databases,
    fake_clock: FakeClock,
) -> None:
    project_id = "prj_activation"
    await databases.project(project_id)

    cadence = Cadence(cron=None, every_minutes=60)
    loc = FileResults(connection="local", path_pattern="out/drift.jsonl")

    view = await checks_store.register(
        project_id,
        check_id="drift_score",
        category="model_performance",
        cadence=cadence,
        timezone="UTC",
        results_location=loc,
        pr_number=42,
        agent="data-quality",
    )

    assert view.id.startswith("chk_")
    assert view.check_id == "drift_score"
    assert view.status == "pending_pr"
    assert view.version is None

    # Verify expectation is initially inactive
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    select(expectations).where(
                        expectations.c.kind == "check",
                        expectations.c.ref == "drift_score",
                    )
                )
            )
            .mappings()
            .one()
        )
        assert row["active"] == 0

    # Advance clock and simulate PR merge
    fake_clock.advance(7200)
    merge_change = PrChange(
        number=42,
        state="merged",
        merged_at=fake_clock.now(),
        head_branch="feature/drift",
        agent="data-quality",
        files=["drift.py"],
    )
    await checks_store.changed(project_id, merge_change)

    # Check should now be active
    active_checks = await checks_store.list_active(project_id)
    assert len(active_checks) == 1
    assert active_checks[0].check_id == "drift_score"
    assert active_checks[0].status == "active"

    # Expectation should now be active with registered_at = merge time
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    select(expectations).where(
                        expectations.c.kind == "check",
                        expectations.c.ref == "drift_score",
                    )
                )
            )
            .mappings()
            .one()
        )
        assert row["active"] == 1


@pytest.mark.asyncio
async def test_merged_check_that_never_writes_raises_silence_signal_past_deadline(
    checks_store: SqliteChecks,
    expectations_service: ExpectationsService,
    fake_incidents: FakeIncidents,
    fake_clock: FakeClock,
    databases: Databases,
) -> None:
    project_id = "prj_silence"
    await set_monitoring_settings(databases, project_id, MonitoringSettings(check_grace_minutes=15))

    cadence = Cadence(cron=None, every_minutes=60)
    loc = FileResults(connection="local", path_pattern="out/drift.jsonl")

    await checks_store.register(
        project_id,
        check_id="drift_score",
        category="model_performance",
        cadence=cadence,
        timezone="UTC",
        results_location=loc,
        pr_number=50,
        agent="data-quality",
    )

    # Merge PR at t0
    merge_change = PrChange(
        number=50,
        state="merged",
        merged_at=fake_clock.now(),
        head_branch="feature/drift",
        agent="data-quality",
        files=["drift.py"],
    )
    await checks_store.changed(project_id, merge_change)

    # Initially inside period: no silence signals
    await expectations_service.evaluate(project_id, fake_clock.now())
    assert len(fake_incidents.signals) == 0

    # Advance past occurrence (60 min) + grace (15 min) -> 80 min (4800s)
    fake_clock.advance(4800)

    await expectations_service.evaluate(project_id, fake_clock.now())
    assert len(fake_incidents.signals) == 1
    project, draft, _seen_at = fake_incidents.signals[0]
    assert project == project_id
    assert draft.kind == "silence"
    assert draft.category == "model_performance"
    assert draft.subject == "drift_score"


@pytest.mark.asyncio
async def test_closed_unmerged_retires_check_and_expectation(
    checks_store: SqliteChecks,
    databases: Databases,
    fake_clock: FakeClock,
) -> None:
    project_id = "prj_retire"
    await databases.project(project_id)

    cadence = Cadence(cron=None, every_minutes=60)
    loc = FileResults(connection="local", path_pattern="out/drift.jsonl")

    await checks_store.register(
        project_id,
        check_id="unwanted_check",
        category="operational",
        cadence=cadence,
        timezone="UTC",
        results_location=loc,
        pr_number=99,
        agent="orchestrator",
    )

    # Close PR without merge
    close_change = PrChange(
        number=99,
        state="closed",
        merged_at=None,
        head_branch="feature/unwanted",
        agent="orchestrator",
        files=["unwanted.py"],
    )
    await checks_store.changed(project_id, close_change)

    # Check is retired
    all_checks = await checks_store.list(project_id)
    assert len(all_checks) == 1
    assert all_checks[0].status == "retired"

    # Expectation is inactive
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    select(expectations).where(
                        expectations.c.kind == "check",
                        expectations.c.ref == "unwanted_check",
                    )
                )
            )
            .mappings()
            .one()
        )
        assert row["active"] == 0


@pytest.mark.asyncio
async def test_reregistering_replaces_check_and_expectation(
    checks_store: SqliteChecks,
    databases: Databases,
) -> None:
    project_id = "prj_reregister"
    await databases.project(project_id)

    cadence1 = Cadence(cron=None, every_minutes=30)
    loc1 = FileResults(connection="local", path_pattern="out/v1.jsonl")

    view1 = await checks_store.register(
        project_id,
        check_id="same_id",
        category="data_quality",
        cadence=cadence1,
        timezone="UTC",
        results_location=loc1,
        pr_number=10,
        agent="data-quality",
    )

    cadence2 = Cadence(cron=None, every_minutes=120)
    loc2 = MlflowResults(metric="custom_drift", threshold=0.05, fires_when="above")

    view2 = await checks_store.register(
        project_id,
        check_id="same_id",
        category="model_performance",
        cadence=cadence2,
        timezone="UTC",
        results_location=loc2,
        pr_number=20,
        agent="model-performance",
    )

    # Replaced: same DB id, updated metadata
    assert view1.id == view2.id
    assert view2.category == "model_performance"
    assert view2.cadence.every_minutes == 120
    assert isinstance(view2.results_location, MlflowResults)
    assert view2.pr_number == 20

    all_checks = await checks_store.list(project_id)
    assert len(all_checks) == 1


@pytest.mark.asyncio
async def test_register_check_tool_contract_and_refuses_malformed(
    checks_store: SqliteChecks,
) -> None:
    tool = RegisterCheck(checks_store)
    assert tool.name == "RegisterCheck"
    assert tool.risk == RiskClass.READ

    ctx = ToolContext(
        project_id="prj_tooltest",
        agent="data-quality",
        session_id="ses_1",
        turn_id="turn_1",
        files=DummyFileAccess(),
        task=None,
        tool_call_id="call_1",
    )

    # 1. Malformed cron via parse
    bad_parsed = tool.parse(
        {
            "check_id": "chk_bad_cron",
            "category": "operational",
            "cadence": {"cron": "* * *"},  # 3 fields instead of 5
            "timezone": "UTC",
            "results_location": {"connection": "local", "path_pattern": "data/*.csv"},
            "pr_number": 1,
        }
    )
    assert not isinstance(bad_parsed, RegisterCheckInput)
    assert bad_parsed.is_error is True

    # Malformed cron expression rejected by croniter in run:
    bad_cron2 = RegisterCheckInput(
        check_id="chk_bad_cron2",
        category="operational",
        cadence=Cadence(cron="65 * * * *", every_minutes=None),  # 65 minute is invalid
        timezone="UTC",
        results_location=FileResults(connection="local", path_pattern="data/*.csv"),
        pr_number=1,
    )
    out = await tool.run(ctx, bad_cron2)
    assert out.is_error is True
    assert "Malformed cron expression" in out.content

    # 2. Malformed timezone
    bad_tz = RegisterCheckInput(
        check_id="chk_bad_tz",
        category="operational",
        cadence=Cadence(cron=None, every_minutes=60),
        timezone="Mars/Phobos",
        results_location=FileResults(connection="local", path_pattern="data/*.csv"),
        pr_number=1,
    )
    out = await tool.run(ctx, bad_tz)
    assert out.is_error is True
    assert "Malformed timezone" in out.content

    # 3. Malformed results location
    bad_loc = RegisterCheckInput(
        check_id="chk_bad_loc",
        category="operational",
        cadence=Cadence(cron=None, every_minutes=60),
        timezone="UTC",
        results_location=FileResults(connection="", path_pattern="data/*.csv"),
        pr_number=1,
    )
    out = await tool.run(ctx, bad_loc)
    assert out.is_error is True
    assert "Malformed file results location" in out.content

    # 4. Valid check registration
    valid_input = RegisterCheckInput(
        check_id="chk_valid",
        category="data_quality",
        cadence=Cadence(cron="*/15 * * * *", every_minutes=None),
        timezone="Europe/Paris",
        results_location=FileResults(connection="local", path_pattern="data/valid.jsonl"),
        pr_number=123,
    )
    out = await tool.run(ctx, valid_input)
    assert out.is_error is False
    assert "Registered check chk_valid" in out.content


@pytest.mark.asyncio
async def test_checks_query_provider_list_and_page(
    checks_store: SqliteChecks,
) -> None:
    project_id = "prj_query"
    provider = ChecksQueryProvider(checks_store)
    assert provider.what == "checks"

    # Register 3 checks
    for i in range(1, 4):
        await checks_store.register(
            project_id,
            check_id=f"chk_metric_{i}",
            category="data_quality",
            cadence=Cadence(cron=None, every_minutes=10 * i),
            timezone="UTC",
            results_location=FileResults(connection="local", path_pattern=f"out/{i}.jsonl"),
            pr_number=100 + i,
            agent="data-quality",
        )

    # Page 1
    page1 = await provider.query(project_id, MonitoringQuery(what="checks", limit=2, offset=0))
    assert page1.total == 3
    assert len(page1.rows) == 2
    assert page1.rows[0].check_id == "chk_metric_1"
    assert page1.rows[1].check_id == "chk_metric_2"

    # Page 2
    page2 = await provider.query(project_id, MonitoringQuery(what="checks", limit=2, offset=2))
    assert page2.total == 3
    assert len(page2.rows) == 1
    assert page2.rows[0].check_id == "chk_metric_3"

    # Filter by subject
    filtered = await provider.query(
        project_id, MonitoringQuery(what="checks", subject="chk_metric_2")
    )
    assert filtered.total == 1
    assert filtered.rows[0].check_id == "chk_metric_2"


@pytest.mark.asyncio
async def test_merged_check_location_becomes_results_source_and_updates_version(
    checks_store: SqliteChecks,
    databases: Databases,
    events: SqliteEventLog,
    fake_clock: FakeClock,
) -> None:
    project_id = "prj_resultswiring"
    connections = FakeConnections()
    handoffs = FakeHandoffs()

    results_service = ResultsService(
        databases=databases,
        handoffs=handoffs,
        connections=connections,
        events=events,
        checks=checks_store,
    )

    cadence = Cadence(cron=None, every_minutes=60)
    loc = FileResults(connection="local", path_pattern="checks/data.jsonl")

    # 1. Register check
    await checks_store.register(
        project_id,
        check_id="latency_check",
        category="operational",
        cadence=cadence,
        timezone="UTC",
        results_location=loc,
        pr_number=77,
        agent="orchestrator",
    )

    # Pending PR: not yet a derived source
    sources_before = await results_service._derive_sources(project_id)  # pyright: ignore[reportPrivateUsage]
    assert not any(s[0] == "file:local:checks/data.jsonl" for s in sources_before)

    # 2. Merge PR
    await checks_store.changed(
        project_id,
        PrChange(
            number=77,
            state="merged",
            merged_at=fake_clock.now(),
            head_branch="feature/latency",
            agent="orchestrator",
            files=["latency.py"],
        ),
    )

    # Active check: now in derived sources!
    sources_after = await results_service._derive_sources(project_id)  # pyright: ignore[reportPrivateUsage]
    assert any(s[0] == "file:local:checks/data.jsonl" for s in sources_after)

    # 3. Simulate a result record stored for this check with version "v1.2.3"
    await results_service.store.insert_items(
        project_id,
        "file:local:checks/data.jsonl",
        [
            (
                "rec_1",
                ResultRecord(
                    check_id="latency_check",
                    origin="team",
                    ran_at=fake_clock.now(),
                    fired=False,
                    value=12.5,
                    threshold=50.0,
                    category="operational",
                    subject="service",
                    model=None,
                    detail={"p99": 12.5},
                    source_ref="file:checks/data.jsonl:local",
                    version="v1.2.3",
                ),
            )
        ],
        seen_at=fake_clock.now(),
    )

    # Verify check view has version populated
    views = await checks_store.list(project_id)
    assert len(views) == 1
    assert views[0].check_id == "latency_check"
    assert views[0].version == "v1.2.3"
