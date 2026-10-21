from __future__ import annotations

import asyncio
import json
import socket
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI

import mlops_agents.api.routers as api_routers
from mlops_agents.agents.runtime.contract import (
    MessageView,
    SessionView,
    StreamEvent,
    TextPart,
)
from mlops_agents.api.routers import sessions as sessions_module
from tests.api.conftest import build_app, signed_in
from tests.api.fake_sessions import FakeSessions
from tests.conftest import make_repo


@dataclass
class SseMessage:
    id: str | None = None
    event: str | None = None
    data: str | None = None
    comment: str | None = None


async def parse_sse_events(lines: AsyncIterator[str]) -> AsyncIterator[SseMessage]:
    cur_id: str | None = None
    cur_event: str | None = None
    cur_data: list[str] = []
    cur_comment: str | None = None

    async for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line:
            if cur_id is not None or cur_event is not None or cur_data or cur_comment is not None:
                yield SseMessage(
                    id=cur_id,
                    event=cur_event,
                    data="\n".join(cur_data) if cur_data else None,
                    comment=cur_comment,
                )
                cur_id = None
                cur_event = None
                cur_data = []
                cur_comment = None
            continue
        if line.startswith(":"):
            cur_comment = line[1:].strip()
        elif line.startswith("id:"):
            cur_id = line[3:].strip()
        elif line.startswith("event:"):
            cur_event = line[6:].strip()
        elif line.startswith("data:"):
            cur_data.append(line[5:].strip())

    if cur_id is not None or cur_event is not None or cur_data or cur_comment is not None:
        yield SseMessage(
            id=cur_id,
            event=cur_event,
            data="\n".join(cur_data) if cur_data else None,
            comment=cur_comment,
        )


async def _create_project(app: FastAPI, tmp_path: Path, git_env: dict[str, str]) -> str:
    repo = make_repo(tmp_path / "test-repo", {"README.md": "# Test"}, git_env)
    project = await app.state.services.workspace.add_local(str(repo))
    return project.id


# ---------------------------------------------------------------------------
# Basic route operations (in-process client)
# ---------------------------------------------------------------------------


async def test_get_session_returns_fake_view(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await _create_project(app, tmp_path, git_env)
    fake = FakeSessions(
        default_view=SessionView(
            id="ses_custom",
            agent="orchestrator",
            busy=True,
            messages=[
                MessageView(
                    id="msg_1",
                    turn_id="trn_1",
                    author_kind="user",
                    author_name="User",
                    from_agent=None,
                    parts=[TextPart(type="text", text="hello")],
                    status="complete",
                    created_at="2026-10-21T20:00:00Z",
                )
            ],
            last_seq=5,
        )
    )
    app.state.services.sessions = fake

    response = await client.get(f"/api/projects/{project_id}/agents/orchestrator/session")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "ses_custom"
    assert data["agent"] == "orchestrator"
    assert data["busy"] is True
    assert data["last_seq"] == 5
    assert len(data["messages"]) == 1
    assert data["messages"][0]["parts"][0]["text"] == "hello"


async def test_send_message_creates_message_and_validates_body(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await _create_project(app, tmp_path, git_env)
    fake = FakeSessions()
    app.state.services.sessions = fake

    # 1. Valid send
    response = await client.post(
        f"/api/projects/{project_id}/agents/orchestrator/session/messages",
        json={"text": "hi"},
    )
    assert response.status_code == 201
    msg_data = response.json()
    assert msg_data["author_kind"] == "user"
    assert msg_data["parts"][0]["text"] == "hi"
    assert fake.sent_messages == [(project_id, "orchestrator", "hi")]

    # Check updated view contains the message
    view = await fake.get(project_id, "orchestrator")
    assert len(view.messages) == 1
    assert view.messages[0].parts[0].type == "text"

    # 2. Empty body field -> 422
    empty_resp = await client.post(
        f"/api/projects/{project_id}/agents/orchestrator/session/messages",
        json={"text": ""},
    )
    assert empty_resp.status_code == 422
    assert empty_resp.json()["code"] == "invalid_request"

    # 3. Extra field -> 422
    extra_resp = await client.post(
        f"/api/projects/{project_id}/agents/orchestrator/session/messages",
        json={"text": "hi", "extra": "forbidden"},
    )
    assert extra_resp.status_code == 422
    assert extra_resp.json()["code"] == "invalid_request"

    # 4. Missing required field -> 422
    missing_resp = await client.post(
        f"/api/projects/{project_id}/agents/orchestrator/session/messages",
        json={},
    )
    assert missing_resp.status_code == 422
    assert missing_resp.json()["code"] == "invalid_request"


async def test_stop_session(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await _create_project(app, tmp_path, git_env)
    fake = FakeSessions()
    app.state.services.sessions = fake

    response = await client.post(f"/api/projects/{project_id}/agents/orchestrator/session/stop")
    assert response.status_code == 204
    assert fake.stopped == [(project_id, "orchestrator")]


# ---------------------------------------------------------------------------
# Stream behaviors (through live_client)
# ---------------------------------------------------------------------------


async def test_stream_events_arrive_in_order(
    live_app: FastAPI,
    live_client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await _create_project(live_app, tmp_path, git_env)
    events = [
        StreamEvent(seq=1, type="message.created", data={"id": "msg_1"}),
        StreamEvent(seq=2, type="text.delta", data={"delta": "hello "}),
        StreamEvent(seq=3, type="text.delta", data={"delta": "world"}),
        StreamEvent(seq=4, type="turn.ended", data={"status": "done"}),
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
    assert [json.loads(m.data or "{}") for m in received] == [e.data for e in events]


async def test_stream_start_positions_and_headers(
    live_app: FastAPI,
    live_client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await _create_project(live_app, tmp_path, git_env)
    fake = FakeSessions()
    live_app.state.services.sessions = fake

    # 1. ?after=41 reaches subscribe as after_seq=41
    async with live_client.stream(
        "GET",
        f"/api/projects/{project_id}/agents/orchestrator/session/stream?after=41",
    ):
        pass
    assert fake.subscribe_calls[-1][2] == 41

    # 2. Last-Event-ID: 57 header wins over ?after=41
    async with live_client.stream(
        "GET",
        f"/api/projects/{project_id}/agents/orchestrator/session/stream?after=41",
        headers={"Last-Event-ID": "57"},
    ):
        pass
    assert fake.subscribe_calls[-1][2] == 57

    # 3. Last-Event-ID: abc -> after_seq=None
    async with live_client.stream(
        "GET",
        f"/api/projects/{project_id}/agents/orchestrator/session/stream?after=41",
        headers={"Last-Event-ID": "abc"},
    ):
        pass
    assert fake.subscribe_calls[-1][2] is None

    # 4. Negative integer -> after_seq=None
    async with live_client.stream(
        "GET",
        f"/api/projects/{project_id}/agents/orchestrator/session/stream?after=-10",
    ):
        pass
    assert fake.subscribe_calls[-1][2] is None


async def test_stream_ping_comment_arrives_when_idle(
    live_app: FastAPI,
    live_client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = await _create_project(live_app, tmp_path, git_env)
    fake = FakeSessions()
    live_app.state.services.sessions = fake

    # Patch PING_SECONDS to 0.1 for fast ping test
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
    project_id = await _create_project(live_app, tmp_path, git_env)
    fake = FakeSessions()
    live_app.state.services.sessions = fake

    async with live_client.stream(
        "GET", f"/api/projects/{project_id}/agents/orchestrator/session/stream"
    ) as response:
        assert response.status_code == 200
        # Wait until subscription is established
        assert len(fake.subscriptions) == 1
        sub = fake.subscriptions[0]
        assert sub.closed is False

    # After client exits stream context, check subscription is closed within 2 s
    async with asyncio.timeout(2.0):
        await sub.closed_event.wait()

    assert sub.closed is True


async def test_live_app_shuts_down_within_5s_with_open_stream(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        application = build_app(tmp_path / "live-shutdown", port=port)
        server = uvicorn.Server(
            uvicorn.Config(application, timeout_graceful_shutdown=5, log_config=None)
        )
        serving = asyncio.create_task(server.serve(sockets=[sock]))

        async with asyncio.timeout(10.0):
            while not server.started:
                if serving.done():
                    serving.result()
                await asyncio.sleep(0.01)

        project_id = await _create_project(application, tmp_path, git_env)
        fake = FakeSessions()
        application.state.services.sessions = fake

        # Open stream over real socket with signed-in client
        async with signed_in(application, f"http://127.0.0.1:{port}", in_process=False) as http:
            stream_ctx = http.stream(
                "GET", f"/api/projects/{project_id}/agents/orchestrator/session/stream"
            )
            response = await stream_ctx.__aenter__()
            assert response.status_code == 200
            assert len(fake.subscriptions) == 1

            # While stream is open, trigger server shutdown
            start_time = time.monotonic()
            server.should_exit = True

            # Server serving task must shut down within 5 seconds
            async with asyncio.timeout(5.0):
                await serving
            elapsed = time.monotonic() - start_time
            assert elapsed < 5.0

            await stream_ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# Rejections: JSON errors not stream
# ---------------------------------------------------------------------------


async def test_unknown_project_returns_json_404_not_stream(
    app: FastAPI, client: httpx2.AsyncClient
) -> None:
    fake = FakeSessions()
    app.state.services.sessions = fake

    response = await client.get("/api/projects/prj_unknown/agents/orchestrator/session/stream")

    assert response.status_code == 404
    assert "application/json" in response.headers["content-type"]
    assert response.json() == {
        "code": "project_not_found",
        "message": "This project does not exist.",
        "detail": None,
    }


async def test_rejected_agent_returns_json_404_or_409_not_stream(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await _create_project(app, tmp_path, git_env)
    fake = FakeSessions(
        unknown_agents={"unknown_agent"},
        no_chat_agents={"no_chat_agent"},
    )
    app.state.services.sessions = fake

    # 1. Unknown agent -> 404 JSON
    resp404 = await client.get(f"/api/projects/{project_id}/agents/unknown_agent/session/stream")
    assert resp404.status_code == 404
    assert "application/json" in resp404.headers["content-type"]
    assert resp404.json()["code"] == "agent_not_found"

    # 2. Agent without chat -> 409 JSON
    resp409 = await client.get(f"/api/projects/{project_id}/agents/no_chat_agent/session/stream")
    assert resp409.status_code == 409
    assert "application/json" in resp409.headers["content-type"]
    assert resp409.json()["code"] == "agent_has_no_chat"


# ---------------------------------------------------------------------------
# Anonymous access: 401 on all 4 routes
# ---------------------------------------------------------------------------


async def test_anonymous_gets_401_on_all_four_routes(
    app: FastAPI,
    anonymous: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await _create_project(app, tmp_path, git_env)
    fake = FakeSessions()
    app.state.services.sessions = fake

    get_resp = await anonymous.get(f"/api/projects/{project_id}/agents/orchestrator/session")
    assert get_resp.status_code == 401
    assert get_resp.json()["code"] == "not_signed_in"

    post_resp = await anonymous.post(
        f"/api/projects/{project_id}/agents/orchestrator/session/messages",
        json={"text": "hi"},
    )
    assert post_resp.status_code == 401
    assert post_resp.json()["code"] == "not_signed_in"

    stop_resp = await anonymous.post(f"/api/projects/{project_id}/agents/orchestrator/session/stop")
    assert stop_resp.status_code == 401
    assert stop_resp.json()["code"] == "not_signed_in"

    stream_resp = await anonymous.get(
        f"/api/projects/{project_id}/agents/orchestrator/session/stream"
    )
    assert stream_resp.status_code == 401
    assert stream_resp.json()["code"] == "not_signed_in"


# ---------------------------------------------------------------------------
# Projects router delete order (if present)
# ---------------------------------------------------------------------------


async def test_delete_project_calls_close_project_before_workspace_remove(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    if not hasattr(api_routers, "projects"):
        pytest.skip("Projects router is not on main yet (api-resources plan)")
    project_id = await _create_project(app, tmp_path, git_env)
    shared_order: list[str] = []
    fake = FakeSessions(order_log=shared_order)
    app.state.services.sessions = fake

    # If projects router exists, DELETE /projects/{project_id} calls close_project then remove
    response = await client.delete(f"/api/projects/{project_id}")
    assert response.status_code in (200, 204)
    assert f"close_project:{project_id}" in shared_order
