"""Tests for project events routes (/api/projects/{id}/events/stream)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from tests.api.conftest import add_local_project
from tests.api.fake_sessions import SseMessage, parse_sse_events


@pytest.mark.asyncio
async def test_events_stream_arrives_in_order(
    live_app: FastAPI,
    live_client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(live_app, tmp_path, git_env)
    events_service = live_app.state.services.events

    # Record 2 initial events
    await events_service.record(project_id, source="user", type="turn.started", payload={"turn": 1})
    await events_service.record(
        project_id, source="orchestrator", type="message.sent", payload={"text": "hello"}
    )

    async with live_client.stream("GET", f"/api/projects/{project_id}/events/stream") as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"

        # Record a 3rd event while stream is open
        await events_service.record(
            project_id,
            source="orchestrator",
            type="turn.ended",
            payload={"turn": 1, "status": "done"},
        )

        received: list[SseMessage] = []
        async for msg in parse_sse_events(response.aiter_lines()):
            if msg.event is not None:
                received.append(msg)
                if len(received) == 1:
                    break

    # Stream with no after_seq starts from live events (event 3)
    assert len(received) == 1
    assert received[0].id == "3"
    assert received[0].event == "turn.ended"
    data = json.loads(received[0].data or "{}")
    assert data["seq"] == 3
    assert data["type"] == "turn.ended"
    assert data["payload"]["status"] == "done"


@pytest.mark.asyncio
async def test_events_stream_resumes_from_last_event_id(
    live_app: FastAPI,
    live_client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(live_app, tmp_path, git_env)
    events_service = live_app.state.services.events

    # Record events 1, 2, 3
    await events_service.record(project_id, source="user", type="turn.started", payload={"n": 1})
    await events_service.record(
        project_id, source="orchestrator", type="message.sent", payload={"n": 2}
    )
    await events_service.record(
        project_id, source="orchestrator", type="turn.ended", payload={"n": 3}
    )

    # Reconnect with Last-Event-ID: 1 -> should replay events 2 and 3
    async with live_client.stream(
        "GET",
        f"/api/projects/{project_id}/events/stream",
        headers={"Last-Event-ID": "1"},
    ) as response:
        assert response.status_code == 200
        received: list[SseMessage] = []
        async for msg in parse_sse_events(response.aiter_lines()):
            if msg.event is not None:
                received.append(msg)
                if len(received) == 2:
                    break

    assert len(received) == 2
    assert [m.id for m in received] == ["2", "3"]
    assert [m.event for m in received] == ["message.sent", "turn.ended"]


@pytest.mark.asyncio
async def test_events_stream_resumes_from_query_after(
    live_app: FastAPI,
    live_client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(live_app, tmp_path, git_env)
    events_service = live_app.state.services.events

    await events_service.record(project_id, source="user", type="turn.started", payload={"n": 1})
    await events_service.record(
        project_id, source="orchestrator", type="turn.ended", payload={"n": 2}
    )

    # Reconnect with ?after=0 -> replays events 1 and 2
    async with live_client.stream(
        "GET", f"/api/projects/{project_id}/events/stream?after=0"
    ) as response:
        assert response.status_code == 200
        received: list[SseMessage] = []
        async for msg in parse_sse_events(response.aiter_lines()):
            if msg.event is not None:
                received.append(msg)
                if len(received) == 2:
                    break

    assert [m.id for m in received] == ["1", "2"]


@pytest.mark.asyncio
async def test_events_stream_unknown_position_resync(
    live_app: FastAPI,
    live_client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(live_app, tmp_path, git_env)
    events_service = live_app.state.services.events

    await events_service.record(project_id, source="user", type="turn.started", payload={"n": 1})

    # Request with Last-Event-ID beyond max seq (999 > 1) -> yields resync
    async with live_client.stream(
        "GET",
        f"/api/projects/{project_id}/events/stream",
        headers={"Last-Event-ID": "999"},
    ) as response:
        assert response.status_code == 200
        received: list[SseMessage] = []
        async for msg in parse_sse_events(response.aiter_lines()):
            if msg.event is not None:
                received.append(msg)
                break

    assert len(received) == 1
    assert received[0].event == "resync"
    assert received[0].id == "1"


@pytest.mark.asyncio
async def test_events_stream_unknown_project_404(
    client: httpx2.AsyncClient,
) -> None:
    resp = await client.get("/api/projects/prj_nonexistent/events/stream")
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/json"
