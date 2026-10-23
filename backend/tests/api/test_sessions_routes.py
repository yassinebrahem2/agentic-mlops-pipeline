from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from mlops_agents.agents.runtime.contract import MessageView, SessionView, TextPart
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.storage.databases import Databases
from mlops_agents.workspace.service import WorkspaceService
from tests.api.conftest import LOCAL_URL, add_local_project, signed_in, started_app
from tests.api.fake_sessions import FakeSessions


async def test_lifespan_builds_real_sessions_and_closes_before_databases(
    tmp_path: Path, git_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    close_sessions = SessionManager.close
    dispose_databases = Databases.dispose

    async def close(manager: SessionManager) -> None:
        await close_sessions(manager)
        order.append("sessions")

    async def dispose(databases: Databases) -> None:
        order.append("databases")
        await dispose_databases(databases)

    monkeypatch.setattr(SessionManager, "close", close)
    monkeypatch.setattr(Databases, "dispose", dispose)
    async with started_app(tmp_path / "real-runtime") as application:
        assert isinstance(application.state.services.sessions, SessionManager)
        project_id = await add_local_project(application, tmp_path, git_env)
        async with signed_in(application, LOCAL_URL) as http:
            response = await http.get(f"/api/projects/{project_id}/agents/orchestrator/session")
        assert response.status_code == 200
        assert response.json()["agent"] == "orchestrator"
        assert response.json()["messages"] == []
        assert response.json()["busy"] is False
        assert order == []

    assert order == ["sessions", "databases"]


async def test_get_session_returns_fake_view(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
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
    assert response.json() == (await fake.get(project_id, "orchestrator")).model_dump()


async def test_send_message_creates_message_and_validates_body(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    fake = FakeSessions()
    app.state.services.sessions = fake

    response = await client.post(
        f"/api/projects/{project_id}/agents/orchestrator/session/messages",
        json={"text": "hi"},
    )
    assert response.status_code == 201
    msg_data = response.json()
    assert msg_data["author_kind"] == "user"
    assert msg_data["parts"][0]["text"] == "hi"
    assert fake.sent_messages == [(project_id, "orchestrator", "hi")]

    view = await fake.get(project_id, "orchestrator")
    assert len(view.messages) == 1
    assert view.messages[0].parts[0].type == "text"

    for body in ({"text": ""}, {"text": "hi", "extra": "forbidden"}, {}):
        invalid_resp = await client.post(
            f"/api/projects/{project_id}/agents/orchestrator/session/messages", json=body
        )
        assert invalid_resp.status_code == 422
        assert invalid_resp.json()["code"] == "invalid_request"


async def test_stop_session(
    app: FastAPI, client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    fake = FakeSessions()
    app.state.services.sessions = fake

    response = await client.post(f"/api/projects/{project_id}/agents/orchestrator/session/stop")
    assert response.status_code == 204
    assert fake.stopped == [(project_id, "orchestrator")]


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
    project_id = await add_local_project(app, tmp_path, git_env)
    fake = FakeSessions(
        unknown_agents={"unknown_agent"},
        no_chat_agents={"no_chat_agent"},
    )
    app.state.services.sessions = fake

    resp404 = await client.get(f"/api/projects/{project_id}/agents/unknown_agent/session/stream")
    assert resp404.status_code == 404
    assert "application/json" in resp404.headers["content-type"]
    assert resp404.json()["code"] == "agent_not_found"

    resp409 = await client.get(f"/api/projects/{project_id}/agents/no_chat_agent/session/stream")
    assert resp409.status_code == 409
    assert "application/json" in resp409.headers["content-type"]
    assert resp409.json()["code"] == "agent_has_no_chat"


async def test_anonymous_gets_401_on_all_four_routes(
    app: FastAPI,
    anonymous: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    fake = FakeSessions()
    app.state.services.sessions = fake

    for method, suffix in (
        ("GET", ""),
        ("POST", "/messages"),
        ("POST", "/stop"),
        ("GET", "/stream"),
    ):
        response = await anonymous.request(
            method,
            f"/api/projects/{project_id}/agents/orchestrator/session{suffix}",
            json={"text": "hi"} if suffix == "/messages" else None,
        )
        assert response.status_code == 401
        assert response.json()["code"] == "not_signed_in"


async def test_delete_project_calls_close_project_before_workspace_remove(
    app: FastAPI,
    client: httpx2.AsyncClient,
    tmp_path: Path,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = await add_local_project(app, tmp_path, git_env)
    shared_order: list[str] = []
    fake = FakeSessions(order_log=shared_order)
    app.state.services.sessions = fake
    remove_project = WorkspaceService.remove

    async def remove(workspace: WorkspaceService, project_id: str) -> None:
        shared_order.append(f"remove:{project_id}")
        await remove_project(workspace, project_id)

    monkeypatch.setattr(WorkspaceService, "remove", remove)

    response = await client.delete(f"/api/projects/{project_id}")
    assert response.status_code == 204
    assert shared_order == [f"close_project:{project_id}", f"remove:{project_id}"]
    assert not app.state.services.paths.project_dir(project_id).exists()
    assert (await client.get(f"/api/projects/{project_id}")).status_code == 404
