from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.agents.runtime.contract import (
    MessageCreated,
    MessageData,
    MessageView,
    StreamEvent,
    TextDelta,
    TextDeltaData,
    TextPart,
    TurnEnded,
    TurnEndedData,
)
from mlops_agents.api.routers import sessions as sessions_module
from tests.api.conftest import add_local_project, live_server, signed_in
from tests.api.fake_sessions import FakeSessions, SseMessage, parse_sse_events


async def _subscribed(fake: FakeSessions) -> None:
    """The server runs on this loop and subscribes after the response headers are out."""
    await asyncio.wait_for(fake.subscribed.wait(), 2.0)


async def test_stream_events_arrive_in_order(
    live_app: FastAPI,
    live_client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(live_app, tmp_path, git_env)
    events: list[StreamEvent] = [
        MessageCreated(
            seq=1,
            type="message.created",
            data=MessageData(
                message=MessageView(
                    id="msg_1",
                    turn_id="trn_1",
                    author_kind="agent",
                    author_name="Orchestrator",
                    from_agent=None,
                    parts=[TextPart(type="text", text="hello")],
                    status="streaming",
                    created_at="2026-10-21T20:00:00Z",
                )
            ),
        ),
        TextDelta(
            seq=2,
            type="text.delta",
            data=TextDeltaData(message_id="msg_1", text="hello "),
        ),
        TextDelta(
            seq=3,
            type="text.delta",
            data=TextDeltaData(message_id="msg_1", text="world"),
        ),
        TurnEnded(
            seq=4,
            type="turn.ended",
            data=TurnEndedData(turn_id="trn_1", status="done", error=None),
        ),
    ]
    fake = FakeSessions(scripted_events=events)
    live_app.state.services.sessions = fake

    async with live_client.stream(
        "GET", f"/api/projects/{project_id}/agents/orchestrator/session/stream"
    ) as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"

        received: list[SseMessage] = []
        async for msg in parse_sse_events(response.aiter_lines()):
            if msg.event is not None:
                received.append(msg)
                if len(received) == 4:
                    break

    assert len(received) == 4
    assert [m.id for m in received] == ["1", "2", "3", "4"]
    assert [m.event for m in received] == [
        "message.created",
        "text.delta",
        "text.delta",
        "turn.ended",
    ]
    assert [json.loads(m.data or "{}") for m in received] == [e.data.model_dump() for e in events]


@pytest.mark.parametrize(
    ("query", "headers", "expected"),
    [
        ("?after=41", {}, 41),
        ("?after=41", {"Last-Event-ID": "57"}, 57),
        ("?after=41", {"Last-Event-ID": "abc"}, None),
        ("?after=-10", {}, None),
        ("", {}, None),
        ("?after=0", {}, 0),
        ("?after=1_0", {}, None),
        ("?after=%2B5", {}, None),
    ],
)
async def test_stream_start_positions_and_headers(
    live_app: FastAPI,
    live_client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
    query: str,
    headers: dict[str, str],
    expected: int | None,
) -> None:
    project_id = await add_local_project(live_app, tmp_path, git_env)
    fake = FakeSessions()
    live_app.state.services.sessions = fake

    async with live_client.stream(
        "GET",
        f"/api/projects/{project_id}/agents/orchestrator/session/stream{query}",
        headers=headers,
    ):
        await _subscribed(fake)
    assert fake.subscribe_calls == [(project_id, "orchestrator", expected)]


async def test_stream_ping_comment_arrives_when_idle(
    live_app: FastAPI,
    live_client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = await add_local_project(live_app, tmp_path, git_env)
    fake = FakeSessions()
    live_app.state.services.sessions = fake

    monkeypatch.setattr(sessions_module, "PING_SECONDS", 0.1)

    async with live_client.stream(
        "GET", f"/api/projects/{project_id}/agents/orchestrator/session/stream"
    ) as response:
        assert response.status_code == 200
        ping_received = False
        async with asyncio.timeout(3.0):
            async for msg in parse_sse_events(response.aiter_lines()):
                if msg.comment is not None and "ping" in msg.comment:
                    ping_received = True
                    break
        assert ping_received is True


async def test_closing_client_stream_closes_fake_iterator(
    live_app: FastAPI,
    live_client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(live_app, tmp_path, git_env)
    fake = FakeSessions()
    live_app.state.services.sessions = fake

    async with live_client.stream(
        "GET", f"/api/projects/{project_id}/agents/orchestrator/session/stream"
    ) as response:
        assert response.status_code == 200
        await _subscribed(fake)
        sub = fake.subscriptions[0]
        assert sub.closed is False

    async with asyncio.timeout(2.0):
        await sub.closed_event.wait()

    assert sub.closed is True


async def test_live_app_shuts_down_within_5s_with_open_stream(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    async with live_server(tmp_path / "live-shutdown") as live:
        project_id = await add_local_project(live.app, tmp_path, git_env)
        fake = FakeSessions()
        live.app.state.services.sessions = fake
        port = live.app.state.services.settings.port

        async with (
            signed_in(live.app, f"http://127.0.0.1:{port}", in_process=False) as http,
            http.stream(
                "GET", f"/api/projects/{project_id}/agents/orchestrator/session/stream"
            ) as response,
        ):
            assert response.status_code == 200
            await _subscribed(fake)

            started = time.monotonic()
            async with asyncio.timeout(5.0):
                await live.stop()
            assert time.monotonic() - started < 5.0
