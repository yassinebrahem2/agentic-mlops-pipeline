"""Tests for project events routes (/api/projects/{id}/events/stream)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import insert

from mlops_agents.agents.tools.base import RiskClass
from mlops_agents.storage.tables import provider_keys, providers
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


@pytest.mark.asyncio
async def test_delete_project_calls_events_close_project(
    client: httpx2.AsyncClient,
    app: FastAPI,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    closed: list[str] = []
    original_close = app.state.services.events.close_project

    async def spy_close(pid: str) -> None:
        closed.append(pid)
        await original_close(pid)

    app.state.services.events.close_project = spy_close

    resp = await client.delete(f"/api/projects/{project_id}")
    assert resp.status_code == 204
    assert closed == [project_id]


@pytest.mark.asyncio
async def test_services_audit_scrubs_keystore_secrets(
    app: FastAPI,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    keystore = app.state.services.keystore
    secret_key = "sk-live-secret-999888"
    keystore.set("key:provider_1", secret_key)

    app_engine = await app.state.services.databases.app()
    async with app_engine.begin() as conn:
        await conn.execute(
            insert(providers).values(
                id="prv_test",
                name="test_p",
                kind="hosted",
                base_url="https://api.test.com",
                status="ok",
                created_at="2026-10-24T00:00:00Z",
            )
        )
        await conn.execute(
            insert(provider_keys).values(
                provider_id="prv_test",
                position=0,
                keystore_ref="key:provider_1",
                hint="••••9888",
                created_at="2026-10-24T00:00:00Z",
            )
        )

    record = await app.state.services.audit.record(
        project_id,
        actor="Orchestrator",
        action="Bash",
        risk_class=RiskClass.RUN,
        permission_level="ask",
        decision="allowed",
        target="test",
        detail={"key": secret_key, "url": f"https://api.test.com?k={secret_key}"},
    )

    assert secret_key not in json.dumps(record.detail)
    assert record.detail["key"] == "***"
    assert "***" in record.detail["url"]


@pytest.mark.asyncio
async def test_services_audit_fails_closed_on_keystore_error(
    app: FastAPI, tmp_path: Path, git_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    keystore = app.state.services.keystore
    keystore.set("key:provider_broken", "sk-secret")

    # Insert provider and key
    app_engine = await app.state.services.databases.app()
    async with app_engine.begin() as conn:
        await conn.execute(
            insert(providers).values(
                id="prv_broken",
                name="test_broken",
                kind="hosted",
                base_url="https://api.broken.com",
                status="ok",
                created_at="2026-10-24T00:00:00Z",
            )
        )
        await conn.execute(
            insert(provider_keys).values(
                provider_id="prv_broken",
                position=0,
                keystore_ref="key:provider_broken",
                hint="••••secret",
                created_at="2026-10-24T00:00:00Z",
            )
        )

    # Invalidate cache
    cache_attr = "_cached_secrets"
    setattr(app.state.services.providers, cache_attr, None)

    class BrokenKeystoreError(RuntimeError):
        pass

    def broken_get(ref: str) -> str:
        raise BrokenKeystoreError("keystore corruption")

    monkeypatch.setattr(keystore, "get", broken_get)

    with pytest.raises(BrokenKeystoreError):
        await app.state.services.audit.record(
            project_id,
            actor="Orchestrator",
            action="Bash",
            risk_class=RiskClass.RUN,
            permission_level="ask",
            decision="allowed",
            target="test",
            detail={"data": "test"},
        )


@pytest.mark.asyncio
async def test_get_agents_status_nonexistent_project(
    live_client: httpx2.AsyncClient,
) -> None:
    response = await live_client.get("/api/projects/prj_missing/agents/status")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_agents_status_reflects_events(
    live_app: FastAPI,
    live_client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = await add_local_project(live_app, tmp_path, git_env)
    events_service = live_app.state.services.events

    # Initially, idle
    res = await live_client.get(f"/api/projects/{project_id}/agents/status")
    assert res.status_code == 200
    data: list[dict[str, Any]] = res.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    # Orchestrator is in the catalog and first
    orch = data[0]
    assert orch["agent"] == "orchestrator"
    assert orch["state"] == "idle"
    assert orch["last_finished"] is None

    # Running turn reported by sessions
    async def mock_turn_states_running(pid: str) -> dict[str, str]:
        return {"orchestrator": "running"}

    monkeypatch.setattr(live_app.state.services.sessions, "turn_states", mock_turn_states_running)
    res = await live_client.get(f"/api/projects/{project_id}/agents/status")
    assert res.status_code == 200
    data_running: list[dict[str, Any]] = res.json()
    assert data_running[0]["state"] == "running"

    # Idle again, record turn.ended event
    async def mock_turn_states_idle(pid: str) -> dict[str, str]:
        return {}

    monkeypatch.setattr(live_app.state.services.sessions, "turn_states", mock_turn_states_idle)
    await events_service.record(
        project_id,
        source="orchestrator",
        type="turn.ended",
        subject_id="trn_1",
        payload={"turn_id": "trn_1", "status": "done", "final_text": "Completed successfully."},
    )
    res = await live_client.get(f"/api/projects/{project_id}/agents/status")
    assert res.status_code == 200
    data_ended: list[dict[str, Any]] = res.json()
    assert data_ended[0]["state"] == "idle"
    assert data_ended[0]["last_finished"] == "Completed successfully."
