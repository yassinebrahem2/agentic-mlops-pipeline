"""Tests for ResultsStore database operations (I22)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from mlops_agents.monitoring.results.contract import (
    ResultQuery,
    ResultRecord,
    SourceState,
    source_id,
)
from mlops_agents.monitoring.results.store import ResultsStore
from mlops_agents.storage.databases import Databases


def _make_record(
    check_id: str = "chk_test",
    ran_at: datetime | None = None,
    origin: str = "agent",
    fired: bool = False,
    subject: str = "features.s",
    model: str | None = "model_a",
    detail: dict[str, Any] | None = None,
) -> ResultRecord:
    return ResultRecord(
        check_id=check_id,
        origin=origin,  # type: ignore
        ran_at=ran_at or datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC),
        fired=fired,
        value=0.05,
        threshold=0.10,
        category="data_quality",
        subject=subject,
        model=model,
        detail=detail if detail is not None else {"n": 1},
        source_ref="checks/test.py",
        version="1.0",
        not_enough_data=False,
    )


async def test_idempotent_ingest_and_conflict_handling(
    databases: Databases, project_id: str
) -> None:
    store = ResultsStore(databases)
    s_id = source_id("file", "*.jsonl", "data_conn")
    t0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)

    items = [
        ("file1:0", _make_record(check_id="chk_1", ran_at=t0)),
        ("file1:100", _make_record(check_id="chk_2", ran_at=t0 + timedelta(minutes=1))),
    ]

    inserted = await store.insert_items(project_id, s_id, items, seen_at=t0)
    assert len(inserted) == 2
    assert inserted[0].id.startswith("res_")
    assert inserted[0].source_key == "file1:0"
    assert inserted[1].source_key == "file1:100"

    # Ingest same items again -> returns 0 newly inserted rows
    duplicate_inserted = await store.insert_items(project_id, s_id, items, seen_at=t0)
    assert len(duplicate_inserted) == 0

    # Ingest 1 existing and 1 new item -> returns only the new item
    mixed_items = [
        ("file1:0", _make_record(check_id="chk_1", ran_at=t0)),
        ("file1:200", _make_record(check_id="chk_3", ran_at=t0 + timedelta(minutes=2))),
    ]
    partial_inserted = await store.insert_items(project_id, s_id, mixed_items, seen_at=t0)
    assert len(partial_inserted) == 1
    assert partial_inserted[0].source_key == "file1:200"
    assert partial_inserted[0].check_id == "chk_3"


async def test_source_state_persistence(databases: Databases, project_id: str) -> None:
    store = ResultsStore(databases)
    s_id = source_id("file", "*.jsonl", "data_conn")
    t0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)

    state = SourceState(
        source_id=s_id,
        kind="file",
        cursor={"files": {"test.jsonl": {"size": 100, "mtime": 123.4, "offset": 100}}},
        last_read=t0,
        last_error="something broke",
        consecutive_failures=2,
    )

    await store.save_source_state(project_id, state)

    loaded = await store.get_source_state(project_id, s_id)
    assert loaded is not None
    assert loaded.source_id == s_id
    assert loaded.kind == "file"
    assert loaded.cursor == state.cursor
    assert loaded.last_read == t0
    assert loaded.last_error == "something broke"
    assert loaded.consecutive_failures == 2


async def test_delivery_tracking(databases: Databases, project_id: str) -> None:
    store = ResultsStore(databases)
    s_id = source_id("file", "*.jsonl", "data_conn")
    t0 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)

    items = [
        ("f:1", _make_record(check_id="chk_2", ran_at=t0 + timedelta(minutes=10))),
        ("f:2", _make_record(check_id="chk_1", ran_at=t0)),
    ]
    await store.insert_items(project_id, s_id, items, seen_at=t0)

    # Fetch undelivered -> sorted by ran_at ASC
    undelivered = await store.fetch_undelivered(project_id)
    assert len(undelivered) == 2
    assert undelivered[0].check_id == "chk_1"
    assert undelivered[1].check_id == "chk_2"

    # Mark delivered
    now = datetime(2026, 10, 30, 11, 0, 0, tzinfo=UTC)
    await store.mark_delivered(project_id, [r.id for r in undelivered], delivered_at=now)

    # Fetch undelivered again -> empty
    undelivered_after = await store.fetch_undelivered(project_id)
    assert len(undelivered_after) == 0


async def test_query_filters_and_latest(databases: Databases, project_id: str) -> None:
    store = ResultsStore(databases)
    s_id = source_id("file", "*.jsonl", "data_conn")
    base_t = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)

    items = [
        ("k1", _make_record(check_id="c1", model="m1", ran_at=base_t, fired=False, subject="s1")),
        (
            "k2",
            _make_record(
                check_id="c1",
                model="m1",
                ran_at=base_t + timedelta(hours=1),
                fired=True,
                subject="s1",
            ),
        ),
        (
            "k3",
            _make_record(
                check_id="c1",
                model="m2",
                ran_at=base_t + timedelta(hours=2),
                fired=False,
                subject="s1",
            ),
        ),
        (
            "k4",
            _make_record(
                check_id="c2",
                model="m1",
                ran_at=base_t + timedelta(hours=3),
                fired=True,
                subject="s2",
            ),
        ),
    ]
    stored = await store.insert_items(project_id, s_id, items, seen_at=base_t)

    # 1. Filter by check_id
    p_check = await store.query(project_id, ResultQuery(check_id="c1"))
    assert p_check.total == 3
    assert len(p_check.rows) == 3

    # 2. Filter by fired
    p_fired = await store.query(project_id, ResultQuery(fired=True))
    assert p_fired.total == 2
    assert {r.source_key for r in p_fired.rows} == {"k2", "k4"}

    # 3. Filter by since/until inclusive
    p_time = await store.query(
        project_id,
        ResultQuery(
            since=base_t + timedelta(hours=1),
            until=base_t + timedelta(hours=2),
        ),
    )
    assert p_time.total == 2
    assert {r.source_key for r in p_time.rows} == {"k2", "k3"}

    # 4. Filter by ids
    target_ids = [stored[0].id, stored[3].id]
    p_ids = await store.query(project_id, ResultQuery(ids=target_ids))
    assert p_ids.total == 2
    assert {r.id for r in p_ids.rows} == set(target_ids)

    # 5. latest=True: newest per (check_id, model)
    # (c1, m1) -> k2
    # (c1, m2) -> k3
    # (c2, m1) -> k4
    p_latest = await store.query(project_id, ResultQuery(latest=True))
    assert p_latest.total == 3
    assert {r.source_key for r in p_latest.rows} == {"k2", "k3", "k4"}


async def test_query_pagination_over_120_rows(databases: Databases, project_id: str) -> None:
    store = ResultsStore(databases)
    s_id = source_id("file", "*.jsonl", "data_conn")
    base_t = datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC)

    items = [
        (f"key_{i:03d}", _make_record(check_id="chk_page", ran_at=base_t + timedelta(minutes=i)))
        for i in range(120)
    ]
    await store.insert_items(project_id, s_id, items, seen_at=base_t)

    # Page 1: limit 50, offset 0
    p1 = await store.query(project_id, ResultQuery(limit=50, offset=0))
    assert p1.total == 120
    assert len(p1.rows) == 50
    assert p1.truncated is True
    # Newest ran_at first
    assert p1.rows[0].source_key == "key_119"

    # Page 2: limit 50, offset 50
    p2 = await store.query(project_id, ResultQuery(limit=50, offset=50))
    assert p2.total == 120
    assert len(p2.rows) == 50
    assert p2.truncated is True
    assert p2.rows[0].source_key == "key_069"

    # Page 3: limit 50, offset 100
    p3 = await store.query(project_id, ResultQuery(limit=50, offset=100))
    assert p3.total == 120
    assert len(p3.rows) == 20
    assert p3.truncated is False
    assert p3.rows[-1].source_key == "key_000"


def test_query_limit_501_refused() -> None:
    with pytest.raises((ValidationError, ValueError)):
        ResultQuery(limit=501)


async def test_latest_per_check(databases: Databases, project_id: str) -> None:
    store = ResultsStore(databases)
    s_id = source_id("file", "*.jsonl", "data_conn")
    base_t = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)

    items = [
        ("k1", _make_record(check_id="c1", subject="sub1", model="m1", ran_at=base_t)),
        (
            "k2",
            _make_record(
                check_id="c1", subject="sub1", model="m1", ran_at=base_t + timedelta(hours=1)
            ),
        ),
        (
            "k3",
            _make_record(
                check_id="c2", subject="sub1", model="m1", ran_at=base_t + timedelta(minutes=30)
            ),
        ),
        (
            "k4",
            _make_record(
                check_id="c1", subject="sub2", model="m1", ran_at=base_t + timedelta(hours=2)
            ),
        ),
    ]
    await store.insert_items(project_id, s_id, items, seen_at=base_t)

    # For sub1 and m1: newest c1 is k2, newest c2 is k3
    latest = await store.latest_per_check(project_id, subject="sub1", model="m1")
    assert len(latest) == 2
    assert {r.source_key for r in latest} == {"k2", "k3"}

    # For model=None: matches records where model IS NULL
    items_none = [
        ("k5", _make_record(check_id="c3", subject="sub1", model=None, ran_at=base_t)),
    ]
    await store.insert_items(project_id, s_id, items_none, seen_at=base_t)
    latest_none = await store.latest_per_check(project_id, subject="sub1", model=None)
    assert any(r.source_key == "k5" for r in latest_none)


def test_cap_detail() -> None:
    import json

    from mlops_agents.monitoring.results.constants import DETAIL_MAX_BYTES
    from mlops_agents.monitoring.results.store import cap_detail

    huge = {"data": "x" * (DETAIL_MAX_BYTES + 5000)}
    capped = cap_detail(huge)
    assert capped.get("truncated") is True
    assert len(json.dumps(capped)) <= DETAIL_MAX_BYTES
