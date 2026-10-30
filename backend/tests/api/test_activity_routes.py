"""Tests for GET /api/projects/{id}/activity: events and audit records merged by time."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import insert

from mlops_agents.storage.tables import audit, events
from tests.api.conftest import add_local_project


async def _seed_event(
    app: FastAPI,
    project_id: str,
    ts: str,
    *,
    source: str = "orchestrator",
    type: str = "message.sent",
    subject_id: str | None = None,
) -> None:
    engine = await app.state.services.databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            insert(events).values(
                id=f"evt_{ts}",
                ts=ts,
                source=source,
                type=type,
                subject_id=subject_id,
                payload=json.dumps({"at": ts}),
            )
        )


async def _seed_audit(
    app: FastAPI, project_id: str, ts: str, *, actor: str = "orchestrator", action: str = "Read"
) -> None:
    engine = await app.state.services.databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            insert(audit).values(
                id=f"aud_{ts}",
                ts=ts,
                actor=actor,
                action=action,
                risk_class="read",
                permission_level="autonomous",
                decision="allowed",
                target="README.md",
                detail=json.dumps({"path": "README.md"}),
            )
        )


def _ts(n: int) -> str:
    return f"2026-10-30T10:00:{n:02d}.000Z"


def _labels(body: dict[str, Any]) -> list[str]:
    return [
        f"{e['kind']}@{e['ts']}" for e in body["entries"]
    ]  # e.g. event@2026-10-30T10:00:03.000Z


async def _get(client: httpx2.AsyncClient, project_id: str, **params: Any) -> dict[str, Any]:
    res = await client.get(f"/api/projects/{project_id}/activity", params=params)
    assert res.status_code == 200, res.text
    body: dict[str, Any] = res.json()
    return body


@pytest.mark.asyncio
async def test_activity_is_newest_first_across_events_and_audit(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    pid = await add_local_project(app, tmp_path, git_env)
    await _seed_event(app, pid, _ts(1))
    await _seed_audit(app, pid, _ts(2))
    await _seed_event(app, pid, _ts(3), subject_id="inc_01ABC")
    await _seed_audit(app, pid, _ts(4))

    body = await _get(client, pid)

    assert _labels(body) == [
        f"audit@{_ts(4)}",
        f"event@{_ts(3)}",
        f"audit@{_ts(2)}",
        f"event@{_ts(1)}",
    ]
    assert body["next_before"] is None
    event_entry = body["entries"][1]
    assert event_entry["event"]["type"] == "message.sent"
    assert event_entry["incident_id"] == "inc_01ABC"
    assert body["entries"][0]["audit"]["action"] == "Read"
    assert body["entries"][3]["incident_id"] is None


@pytest.mark.asyncio
async def test_paging_has_no_duplicate_and_no_gap_when_records_arrive_between_pages(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    pid = await add_local_project(app, tmp_path, git_env)
    for n in (1, 3, 5, 7):
        await _seed_event(app, pid, _ts(n))
    for n in (2, 4, 6, 8):
        await _seed_audit(app, pid, _ts(n))

    first = await _get(client, pid, limit=3)
    assert _labels(first) == [f"audit@{_ts(8)}", f"event@{_ts(7)}", f"audit@{_ts(6)}"]
    assert first["next_before"] is not None

    # Newer records land between the two page reads; they must not shift the next page.
    await _seed_event(app, pid, _ts(20))
    await _seed_audit(app, pid, _ts(21))

    second = await _get(client, pid, limit=3, before=first["next_before"])
    assert _labels(second) == [f"event@{_ts(5)}", f"audit@{_ts(4)}", f"event@{_ts(3)}"]

    third = await _get(client, pid, limit=3, before=second["next_before"])
    assert _labels(third) == [f"audit@{_ts(2)}", f"event@{_ts(1)}"]
    assert third["next_before"] is None


@pytest.mark.asyncio
async def test_a_stream_with_nothing_on_the_page_keeps_its_position(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    pid = await add_local_project(app, tmp_path, git_env)
    await _seed_event(app, pid, _ts(1))
    for n in (5, 6, 7):
        await _seed_audit(app, pid, _ts(n))

    first = await _get(client, pid, limit=2)
    assert _labels(first) == [f"audit@{_ts(7)}", f"audit@{_ts(6)}"]
    second = await _get(client, pid, limit=2, before=first["next_before"])

    assert _labels(second) == [f"audit@{_ts(5)}", f"event@{_ts(1)}"]
    assert second["next_before"] is None


@pytest.mark.asyncio
async def test_source_matches_event_source_and_audit_actor(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    pid = await add_local_project(app, tmp_path, git_env)
    await _seed_event(app, pid, _ts(1), source="coder")
    await _seed_event(app, pid, _ts(2), source="user")
    await _seed_audit(app, pid, _ts(3), actor="coder")
    await _seed_audit(app, pid, _ts(4), actor="reviewer")

    body = await _get(client, pid, source="coder")

    assert _labels(body) == [f"audit@{_ts(3)}", f"event@{_ts(1)}"]


@pytest.mark.asyncio
async def test_type_filter_returns_only_events_of_that_type(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    pid = await add_local_project(app, tmp_path, git_env)
    await _seed_event(app, pid, _ts(1), type="turn.started")
    await _seed_event(app, pid, _ts(2), type="message.sent")
    await _seed_audit(app, pid, _ts(3))

    body = await _get(client, pid, type="message.sent")

    assert _labels(body) == [f"event@{_ts(2)}"]


@pytest.mark.asyncio
async def test_unknown_project_is_404(client: httpx2.AsyncClient) -> None:
    res = await client.get("/api/projects/prj_missing/activity")
    assert res.status_code == 404
    # The code tells ProjectDep's answer from a bare 404 on a path that has no route.
    assert res.json()["code"] == "project_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"before": "not-a-cursor"},
        {"before": "eyJlIjoieCJ9"},  # valid base64 of {"e":"x"}, not a cursor of ours
        {"type": "no.such.type"},
        {"limit": 0},
        {"limit": 100000},
    ],
)
async def test_bad_query_is_422_never_500(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
    params: dict[str, Any],
) -> None:
    pid = await add_local_project(app, tmp_path, git_env)
    res = await client.get(f"/api/projects/{pid}/activity", params=params)
    assert res.status_code == 422, res.text
