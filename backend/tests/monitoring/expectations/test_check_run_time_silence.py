"""Reproduction test for #645: ran_at is when the check ran, not the data date.

A daily check stamped with its run time meets its occurrence (0 silence signals),
whereas one stamped with the data's date does not (1 silence signal).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from mlops_agents.monitoring.expectations.contract import ExpectationKey, ExpectationSpec
from mlops_agents.monitoring.expectations.service import ExpectationsService
from mlops_agents.monitoring.incidents.contract import SignalDraft, SignalOutcome
from mlops_agents.monitoring.results.contract import StoredResult
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.payloads import Cadence
from mlops_agents.storage.databases import Databases
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


@pytest.fixture
def databases(tmp_path: Path) -> Databases:
    return Databases(DataPaths(tmp_path / "data"))


def _daily_check_spec(check_id: str) -> ExpectationSpec:
    return ExpectationSpec(
        key=ExpectationKey(kind="check", ref=check_id, model=None),
        category="model_performance",
        cadence=Cadence(cron="0 6 * * *", every_minutes=None),
        due_at=None,
        timezone="UTC",
        grace_minutes=30,
        second_deadline=False,
        source="check",
    )


def _make_result(check_id: str, ran_at: datetime, seen_at: datetime) -> StoredResult:
    return StoredResult(
        id="res_1",
        check_id=check_id,
        origin="agent",
        ran_at=ran_at,
        fired=False,
        category="model_performance",
        subject=check_id,
        model=None,
        detail={"data_covered": "2026-10-31"},
        source_ref="checks/drift.py",
        source_id="ci:daily.yml",
        source_key="k1",
        seen_at=seen_at,
    )


@pytest.mark.asyncio
async def test_daily_check_stamped_with_run_time_meets_occurrence_zero_silence(
    databases: Databases,
) -> None:
    project_id = "prj_runtimeok"
    incidents = FakeIncidents()
    clock = FakeClock(start=datetime(2026, 10, 31, 6, 0, 0, tzinfo=UTC))
    service = ExpectationsService(
        databases=databases,
        handoffs=FakeHandoffs(),
        connections=FakeConnections(),
        incidents=incidents,
        clock=clock,
    )

    spec = _daily_check_spec("daily_drift")
    # Registered on previous occurrence: 2026-10-31 06:00 UTC
    await service.register(project_id, spec, active=True)
    await service.note_seen(project_id, spec.key, datetime(2026, 10, 31, 6, 0, 0, tzinfo=UTC))

    # The check runs on 2026-11-01 at 06:05 UTC (covering yesterday's data)
    # Stamped with run time:
    run_time = datetime(2026, 11, 1, 6, 5, 0, tzinfo=UTC)
    result = _make_result("daily_drift", ran_at=run_time, seen_at=run_time)
    await service.results_observer.stored(project_id, [result])

    # Advance clock past 06:00 + 30m grace to 06:35 UTC
    clock.current = datetime(2026, 11, 1, 6, 35, 0, tzinfo=UTC)
    signals = await service.evaluate(project_id, clock.now())

    assert len(signals) == 0
    assert len(incidents.signals) == 0


@pytest.mark.asyncio
async def test_daily_check_stamped_with_data_date_misses_occurrence_raises_silence(
    databases: Databases,
) -> None:
    project_id = "prj_runtimefail"
    incidents = FakeIncidents()
    clock = FakeClock(start=datetime(2026, 10, 31, 6, 0, 0, tzinfo=UTC))
    service = ExpectationsService(
        databases=databases,
        handoffs=FakeHandoffs(),
        connections=FakeConnections(),
        incidents=incidents,
        clock=clock,
    )

    spec = _daily_check_spec("daily_drift")
    # Registered on previous occurrence: 2026-10-31 06:00 UTC
    await service.register(project_id, spec, active=True)
    await service.note_seen(project_id, spec.key, datetime(2026, 10, 31, 6, 0, 0, tzinfo=UTC))

    # The check runs on 2026-11-01 at 06:05 UTC over yesterday's data,
    # but (as in bug #645) stamps ran_at with the data's date (e.g. 2026-10-31 00:00 or 06:00).
    data_date = datetime(2026, 10, 31, 0, 0, 0, tzinfo=UTC)
    seen_time = datetime(2026, 11, 1, 6, 5, 0, tzinfo=UTC)
    result = _make_result("daily_drift", ran_at=data_date, seen_at=seen_time)
    await service.results_observer.stored(project_id, [result])

    # Advance clock past 06:00 + 30m grace to 06:35 UTC
    clock.current = datetime(2026, 11, 1, 6, 35, 0, tzinfo=UTC)
    signals = await service.evaluate(project_id, clock.now())

    # Occurrence 2026-11-01 06:00 was not met because ran_at is outside (prev_occ, first_deadline]
    assert len(signals) == 1
    assert len(incidents.signals) == 1
    project, draft, _ = incidents.signals[0]
    assert project == project_id
    assert draft.kind == "silence"
    assert draft.subject == "daily_drift"
