"""Tests for SqliteExpectationsStore."""

from datetime import UTC, datetime

import pytest

from mlops_agents.monitoring.expectations.contract import ExpectationKey, ExpectationSpec
from mlops_agents.monitoring.expectations.store import SqliteExpectationsStore
from mlops_agents.proposals.payloads import Cadence
from mlops_agents.storage.databases import Databases
from tests.providers.fake_clock import FakeClock


@pytest.fixture
def project_id() -> str:
    return "prj_teststore"


def _make_spec(
    ref: str,
    kind: str = "feed",
    model: str | None = None,
    source: str = "handoff",
    activates_on_pr: int | None = None,
) -> ExpectationSpec:
    return ExpectationSpec(
        key=ExpectationKey(kind=kind, ref=ref, model=model),  # type: ignore
        category="data_quality",
        cadence=Cadence(cron=None, every_minutes=60),
        timezone="UTC",
        source=source,  # type: ignore
        activates_on_pr=activates_on_pr,
    )


async def test_register_and_get(databases: Databases, project_id: str) -> None:
    clock = FakeClock()
    store = SqliteExpectationsStore(databases, clock)
    spec = _make_spec("sales_feed")
    await store.register(project_id, spec)

    record = await store.get(project_id, spec.key)
    assert record is not None
    loaded_spec, active, last_seen, signalled_first, signalled_second, registered_at = record
    assert loaded_spec.key == spec.key
    assert active is True
    assert last_seen is None
    assert signalled_first is None
    assert signalled_second is None
    assert registered_at == clock.now()

    # Update spec
    updated_spec = _make_spec("sales_feed")
    updated_spec.grace_minutes = 15
    await store.register(project_id, updated_spec)
    record2 = await store.get(project_id, spec.key)
    assert record2 is not None
    assert record2[0].grace_minutes == 15


async def test_retire_and_activate(databases: Databases, project_id: str) -> None:
    store = SqliteExpectationsStore(databases, FakeClock())
    spec = _make_spec("churn_check", kind="check", source="check", activates_on_pr=42)
    # Spec with activates_on_pr registers as inactive
    await store.register(project_id, spec)
    record = await store.get(project_id, spec.key)
    assert record is not None
    assert record[1] is False  # inactive

    activated = await store.activate_for_pr(project_id, 42)
    assert spec.key in activated
    record_after = await store.get(project_id, spec.key)
    assert record_after is not None
    assert record_after[1] is True  # active

    await store.retire(project_id, spec.key)
    record_retired = await store.get(project_id, spec.key)
    assert record_retired is not None
    assert record_retired[1] is False  # retired (active=0)


async def test_note_seen_and_signalled(databases: Databases, project_id: str) -> None:
    store = SqliteExpectationsStore(databases, FakeClock())
    spec = _make_spec("job_eval", kind="job")
    await store.register(project_id, spec)

    t1 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 31, 11, 0, tzinfo=UTC)
    await store.note_seen(project_id, spec.key, t1)
    record = await store.get(project_id, spec.key)
    assert record is not None
    assert record[2] == t1

    # Older timestamp does not overwrite newer
    t0 = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
    await store.note_seen(project_id, spec.key, t0)
    record = await store.get(project_id, spec.key)
    assert record is not None
    assert record[2] == t1

    # Newer timestamp overwrites
    await store.note_seen(project_id, spec.key, t2)
    record = await store.get(project_id, spec.key)
    assert record is not None
    assert record[2] == t2

    # Signalled timestamps
    await store.update_signalled(project_id, spec.key, first_t=t1, second_t=t2)
    record = await store.get(project_id, spec.key)
    assert record is not None
    assert record[3] == t1
    assert record[4] == t2


async def test_retire_handoff_keys_not_in(databases: Databases, project_id: str) -> None:
    store = SqliteExpectationsStore(databases, FakeClock())
    handoff_spec1 = _make_spec("f1", source="handoff")
    handoff_spec2 = _make_spec("f2", source="handoff")
    check_spec = _make_spec("chk_custom", kind="check", source="check")
    training_spec = _make_spec("tr_req", kind="training_request", source="training")

    await store.register(project_id, handoff_spec1, active=True)
    await store.register(project_id, handoff_spec2, active=True)
    await store.register(project_id, check_spec, active=True)
    await store.register(project_id, training_spec, active=True)

    # Current handoff has only f1
    current_keys = {("feed", "f1", "")}
    retired = await store.retire_handoff_keys_not_in(project_id, current_keys)
    assert len(retired) == 1
    assert retired[0] == handoff_spec2.key

    # f1 is still active
    rec_f1 = await store.get(project_id, handoff_spec1.key)
    assert rec_f1 is not None and rec_f1[1] is True
    # f2 is retired
    rec_f2 = await store.get(project_id, handoff_spec2.key)
    assert rec_f2 is not None and rec_f2[1] is False
    # check and training are NEVER retired
    rec_chk = await store.get(project_id, check_spec.key)
    assert rec_chk is not None and rec_chk[1] is True
    rec_tr = await store.get(project_id, training_spec.key)
    assert rec_tr is not None and rec_tr[1] is True
