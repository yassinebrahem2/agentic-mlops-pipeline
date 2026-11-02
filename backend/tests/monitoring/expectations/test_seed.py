"""Tests for seeding expectations from handoffs (seed.py)."""

from datetime import UTC, datetime

import pytest

from mlops_agents.monitoring.expectations.contract import ExpectationKey, ExpectationSpec
from mlops_agents.monitoring.expectations.seed import HandoffExpectationsSeeder
from mlops_agents.monitoring.expectations.store import SqliteExpectationsStore
from mlops_agents.proposals.handoffs import SqliteHandoffs
from mlops_agents.proposals.payloads import Cadence, HandoffPayload
from mlops_agents.storage.databases import Databases
from tests.proposals.samples import data_feed, handoff, job_feed
from tests.providers.fake_clock import FakeClock


@pytest.fixture
def project_id() -> str:
    return "prj_testseed"


async def test_handoff_stored_seeds_feeds_and_checks(databases: Databases, project_id: str) -> None:
    store = SqliteExpectationsStore(databases, FakeClock())
    seeder = HandoffExpectationsSeeder(store)

    check_item = {
        "check_id": "chk_auc",
        "name": "AUC check",
        "category": "model_performance",
        "cadence": {"cron": "0 12 * * *", "every_minutes": None},
        "timezone": "Europe/Paris",
        "results": {
            "connection": "hotel",
            "path_pattern": "results/*.json",
        },
    }
    raw = handoff()
    raw["operations"]["feeds"] = [
        data_feed(name="feed_bookings"),
        job_feed(name="job_nightly"),
    ]
    raw["operations"]["checks"] = [check_item]
    payload = HandoffPayload.model_validate(raw)

    await seeder.seed_from_payload(project_id, payload)

    # 1. Data feed
    feed_key = ExpectationKey(kind="feed", ref="feed_bookings", model=None)
    feed_rec = await store.get(project_id, feed_key)
    assert feed_rec is not None
    feed_spec, active, _, _, _, _ = feed_rec
    assert active is True
    assert feed_spec.category == "data_quality"
    assert feed_spec.second_deadline is True
    assert feed_spec.source == "handoff"
    assert feed_spec.grace_minutes == 60

    # 2. Job feed
    job_key = ExpectationKey(kind="job", ref="job_nightly", model=None)
    job_rec = await store.get(project_id, job_key)
    assert job_rec is not None
    job_spec, active, _, _, _, _ = job_rec
    assert active is True
    assert job_spec.category == "operational"
    assert job_spec.second_deadline is True
    assert job_spec.source == "handoff"
    assert job_spec.grace_minutes == 30

    # 3. Check
    chk_key = ExpectationKey(kind="check", ref="chk_auc", model=None)
    chk_rec = await store.get(project_id, chk_key)
    assert chk_rec is not None
    chk_spec, active, _, _, _, _ = chk_rec
    assert active is True
    assert chk_spec.category == "model_performance"
    assert chk_spec.second_deadline is False
    assert chk_spec.grace_minutes is None
    assert chk_spec.source == "handoff"


async def test_second_handoff_retires_removed_feed(databases: Databases, project_id: str) -> None:
    store = SqliteExpectationsStore(databases, FakeClock())
    seeder = HandoffExpectationsSeeder(store)

    raw1 = handoff()
    raw1["operations"]["feeds"] = [
        data_feed(name="feed_keep"),
        data_feed(name="feed_remove"),
    ]
    await seeder.seed_from_payload(project_id, HandoffPayload.model_validate(raw1))

    # Second handoff removes feed_remove
    raw2 = handoff()
    raw2["operations"]["feeds"] = [data_feed(name="feed_keep")]
    await seeder.seed_from_payload(project_id, HandoffPayload.model_validate(raw2))

    keep_rec = await store.get(project_id, ExpectationKey(kind="feed", ref="feed_keep"))
    assert keep_rec is not None and keep_rec[1] is True

    remove_rec = await store.get(project_id, ExpectationKey(kind="feed", ref="feed_remove"))
    assert remove_rec is not None and remove_rec[1] is False  # retired


async def test_restore_and_restart_leave_check_and_training_request_active(
    databases: Databases, project_id: str
) -> None:
    store = SqliteExpectationsStore(databases, FakeClock())
    seeder = HandoffExpectationsSeeder(store)

    # Register external check and training_request expectations
    chk_spec = ExpectationSpec(
        key=ExpectationKey(kind="check", ref="chk_registered"),
        category="drift",
        cadence=Cadence(cron=None, every_minutes=60),
        timezone="UTC",
        source="check",
    )
    tr_spec = ExpectationSpec(
        key=ExpectationKey(kind="training_request", ref="tr_run_9"),
        category="operational",
        due_at=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
        timezone="UTC",
        source="training",
    )
    await store.register(project_id, chk_spec, active=True)
    await store.register(project_id, tr_spec, active=True)

    # Seed handoff (which doesn't know about chk_registered or tr_run_9)
    raw = handoff()
    raw["operations"]["feeds"] = [data_feed(name="feed_a")]
    raw["operations"]["checks"] = []
    await seeder.seed_from_payload(project_id, HandoffPayload.model_validate(raw))

    chk_rec = await store.get(project_id, chk_spec.key)
    assert chk_rec is not None and chk_rec[1] is True

    tr_rec = await store.get(project_id, tr_spec.key)
    assert tr_rec is not None and tr_rec[1] is True


async def test_startup_reconciles_handoff_stored_before(
    databases: Databases, project_id: str
) -> None:
    handoffs = SqliteHandoffs(databases)
    store = SqliteExpectationsStore(databases, FakeClock())
    seeder = HandoffExpectationsSeeder(store)

    raw = handoff()
    raw["operations"]["feeds"] = [data_feed(name="stored_feed")]
    payload = HandoffPayload.model_validate(raw)
    await handoffs.store(project_id, "prop_1", payload)

    # At startup: reconcile reads current handoff
    current = await handoffs.current(project_id)
    assert current is not None
    await seeder.seed_from_payload(project_id, current.payload)

    rec = await store.get(project_id, ExpectationKey(kind="feed", ref="stored_feed"))
    assert rec is not None and rec[1] is True


async def test_check_without_cadence_registers_no_expectation_and_dropping_cadence_retires_it(
    databases: Databases, project_id: str
) -> None:
    store = SqliteExpectationsStore(databases, FakeClock())
    seeder = HandoffExpectationsSeeder(store)

    check_with_cadence = {
        "check_id": "chk_demand",
        "name": "On-demand check",
        "category": "model_performance",
        "cadence": {"cron": "0 12 * * *", "every_minutes": None},
        "timezone": "Europe/Paris",
        "results": {
            "connection": "hotel",
            "path_pattern": "results/*.json",
        },
    }
    raw1 = handoff()
    raw1["operations"]["feeds"] = []
    raw1["operations"]["checks"] = [check_with_cadence]
    await seeder.seed_from_payload(project_id, HandoffPayload.model_validate(raw1))

    key = ExpectationKey(kind="check", ref="chk_demand")
    rec1 = await store.get(project_id, key)
    assert rec1 is not None and rec1[1] is True

    # Second handoff drops the check's cadence:
    check_no_cadence = {
        "check_id": "chk_demand",
        "name": "On-demand check",
        "category": "model_performance",
        "cadence": None,
        "timezone": "Europe/Paris",
        "results": {
            "connection": "hotel",
            "path_pattern": "results/*.json",
        },
    }
    raw2 = handoff()
    raw2["operations"]["feeds"] = []
    raw2["operations"]["checks"] = [check_no_cadence]
    await seeder.seed_from_payload(project_id, HandoffPayload.model_validate(raw2))

    # The previously registered expectation must be retired
    rec2 = await store.get(project_id, key)
    assert rec2 is not None and rec2[1] is False

    # A brand new check without cadence registers no expectation
    raw3 = handoff()
    raw3["operations"]["feeds"] = []
    raw3["operations"]["checks"] = [
        {
            "check_id": "chk_never_scheduled",
            "name": "Never scheduled",
            "category": "operational",
            "timezone": "UTC",
            "results": {"workflow": "check.yml"},
        }
    ]
    await seeder.seed_from_payload(project_id, HandoffPayload.model_validate(raw3))
    rec3 = await store.get(project_id, ExpectationKey(kind="check", ref="chk_never_scheduled"))
    assert rec3 is None
