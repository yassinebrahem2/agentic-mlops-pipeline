from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from pydantic import TypeAdapter

from mlops_agents.agents.runtime.contract import (
    MessageCreated,
    MessageUpdated,
    MessageView,
    Resync,
    SessionView,
    StreamEvent,
    TextDelta,
    TextPart,
    ToolCallPart,
    ToolFinished,
    ToolResultPart,
    ToolStarted,
    TurnEnded,
)
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.paths import DataPaths
from mlops_agents.workspace.models import Project
from tests.api.fake_sessions import parse_sse_events
from tests.conftest import make_repo
from tests.fakes.fake_openai import SLOW_TEXT, RunningFake

EVENT = TypeAdapter(StreamEvent)
README = "# Live session repository"


@pytest.fixture
async def session_url(
    live_client: httpx2.AsyncClient, tmp_path: Path, git_env: dict[str, str]
) -> str:
    repo = await asyncio.to_thread(
        make_repo, tmp_path / "repo", {"README.md": README + "\nRead through HTTP.\n"}, git_env
    )
    response = await live_client.post(
        "/api/projects", json={"source": "local", "path": str(repo), "init_git": False}
    )
    assert response.status_code == 201, response.text
    project = Project.model_validate(response.json())
    return f"/api/projects/{project.id}/agents/orchestrator/session"


async def _assign_model(http: httpx2.AsyncClient, fake: RunningFake, model: str) -> None:
    response = await http.post(
        "/api/providers",
        json={"name": "fake", "kind": "self_hosted", "base_url": fake.base_url},
    )
    assert response.status_code == 201, response.text
    response = await http.post("/api/models", json={"provider": "fake", "wire_model_id": model})
    assert response.status_code == 201, response.text
    response = await http.post("/api/models/verify", json={"address": f"fake:{model}"})
    assert response.status_code == 200, response.text
    assert response.json()["tool_calling"] is True, response.text
    assert response.json()["streaming"] is True, response.text
    assert response.json()["problem"] is None, response.text
    response = await http.put("/api/agents/orchestrator/model", json={"address": f"fake:{model}"})
    assert response.status_code == 200, response.text
    assert response.json()["settings"]["effective_model"] == f"fake:{model}"


async def _view(http: httpx2.AsyncClient, url: str, *, idle: bool = False) -> SessionView:
    async with asyncio.timeout(10):
        while True:
            response = await http.get(url)
            assert response.status_code == 200, response.text
            view = SessionView.model_validate(response.json())
            if not idle or not view.busy:
                return view
            await asyncio.sleep(0.01)


async def _send(http: httpx2.AsyncClient, url: str, text: str) -> MessageView:
    response = await http.post(url + "/messages", json={"text": text})
    assert response.status_code == 201, response.text
    return MessageView.model_validate(response.json())


async def _decode(response: httpx2.Response) -> AsyncGenerator[StreamEvent]:
    async for frame in parse_sse_events(response.aiter_lines()):
        if frame.event is not None:
            assert frame.id is not None and frame.data is not None
            yield EVENT.validate_python(
                {"seq": int(frame.id), "type": frame.event, "data": json.loads(frame.data)}
            )


@asynccontextmanager
async def _stream(
    http: httpx2.AsyncClient, url: str, *, after: int | None = None, header: bool = False
) -> AsyncGenerator[AsyncIterator[StreamEvent]]:
    headers = {"Last-Event-ID": str(after)} if header else {}
    query = f"?after={after}" if after is not None and not header else ""
    async with http.stream("GET", url + "/stream" + query, headers=headers) as response:
        assert response.status_code == 200, response.status_code
        assert "text/event-stream" in response.headers["content-type"]
        events = _decode(response)
        try:
            if after is None:
                assert isinstance(await _next(events), Resync)
            yield events
        finally:
            await events.aclose()


async def _next(events: AsyncIterator[StreamEvent]) -> StreamEvent:
    async with asyncio.timeout(10):
        return await anext(events)


async def _until_end(events: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    received: list[StreamEvent] = []
    async with asyncio.timeout(15):
        async for event in events:
            received.append(event)
            if isinstance(event, TurnEnded):
                break
    assert received and isinstance(received[-1], TurnEnded)
    return received


def _text(message: MessageView) -> str:
    return "".join(part.text for part in message.parts if isinstance(part, TextPart))


async def test_real_runtime_reads_repository_and_persists_streamed_turn(
    live_app: FastAPI,
    live_client: httpx2.AsyncClient,
    fake_openai: RunningFake,
    session_url: str,
) -> None:
    assert isinstance(live_app.state.services.sessions, SessionManager)
    await _assign_model(live_client, fake_openai, "fake-tools")
    async with _stream(live_client, session_url) as stream:
        sent = await _send(live_client, session_url, "What does the README say?")
        events = await _until_end(stream)

    created = [event.data.message for event in events if isinstance(event, MessageCreated)]
    assert [message.author_kind for message in created] == ["user", "agent"]
    assert created[0] == sent
    started = [event for event in events if isinstance(event, ToolStarted)]
    finished = [event for event in events if isinstance(event, ToolFinished)]
    assert len(started) == len(finished) == 1
    assert started[0].data.name == "Read"
    assert started[0].data.input == {"file_path": "README.md"}
    assert finished[0].data.tool_call_id == started[0].data.tool_call_id
    assert finished[0].data.is_error is False
    assert README in finished[0].data.content
    kinds = [event.type for event in events]
    assert kinds.index("tool.started") < kinds.index("tool.finished") < kinds.index("text.delta")
    assert kinds[-2:] == ["message.updated", "turn.ended"]
    assert isinstance(events[-1], TurnEnded) and events[-1].data.status == "done"
    assert events[-1].data.error is None
    seqs = [event.seq for event in events]
    assert seqs == sorted(set(seqs))

    view = await _view(live_client, session_url, idle=True)
    assert view.busy is False
    assert [message.author_kind for message in view.messages] == ["user", "agent"]
    assert view.messages[0] == sent
    agent = view.messages[1]
    assert agent.status == "complete"
    assert README in _text(agent)
    assert "".join(event.data.text for event in events if isinstance(event, TextDelta)) == _text(
        agent
    )
    updated = [event.data.message for event in events if isinstance(event, MessageUpdated)]
    assert updated == [agent]
    calls = [part for part in agent.parts if isinstance(part, ToolCallPart)]
    results = [part for part in agent.parts if isinstance(part, ToolResultPart)]
    assert len(calls) == len(results) == 1
    assert calls[0].id == results[0].tool_call_id == started[0].data.tool_call_id
    assert calls[0].name == "Read" and calls[0].input == started[0].data.input
    assert results[0].content == finished[0].data.content and not results[0].is_error
    assert view.last_seq == events[-1].seq


async def test_no_assigned_model_streams_failure_and_actionable_system_message(
    live_client: httpx2.AsyncClient, session_url: str
) -> None:
    async with _stream(live_client, session_url) as stream:
        sent = await _send(live_client, session_url, "Hello")
        events = await _until_end(stream)

    ended = events[-1]
    assert isinstance(ended, TurnEnded) and ended.data.status == "failed"
    created = [event.data.message for event in events if isinstance(event, MessageCreated)]
    assert [message.author_kind for message in created] == ["user", "system"]
    assert created[0] == sent
    assert "choose a model" in _text(created[1]).lower()
    assert "AI providers" in _text(created[1])
    assert ended.data.error == _text(created[1])
    view = await _view(live_client, session_url, idle=True)
    assert view.busy is False
    assert view.messages == created
    assert view.last_seq == ended.seq


async def _through_delta(events: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    received: list[StreamEvent] = []
    async with asyncio.timeout(10):
        while True:
            event = await anext(events)
            received.append(event)
            assert not isinstance(event, TurnEnded), received
            if isinstance(event, TextDelta) and event.data.text:
                return received


def _deltas(events: list[StreamEvent]) -> str:
    return "".join(event.data.text for event in events if isinstance(event, TextDelta))


async def test_two_tabs_and_mid_turn_snapshot_converge_without_missing_or_doubled_text(
    live_client: httpx2.AsyncClient, fake_openai: RunningFake, session_url: str
) -> None:
    await _assign_model(live_client, fake_openai, "fake-slow")
    async with (
        _stream(live_client, session_url) as first,
        _stream(live_client, session_url) as second,
    ):
        await _send(live_client, session_url, "Reply slowly")
        prefix = await _through_delta(first)
        snapshot = await _view(live_client, session_url)
        assert snapshot.busy
        message = snapshot.messages[-1]
        assert message.author_kind == "agent" and message.status == "streaming"
        assert 0 < len(_text(message)) < len(SLOW_TEXT)
        async with (
            _stream(live_client, session_url, after=snapshot.last_seq) as third,
            asyncio.TaskGroup() as readers,
        ):
            first_tail = readers.create_task(_until_end(first))
            second_turn = readers.create_task(_until_end(second))
            third_tail = readers.create_task(_until_end(third))

    first_events = prefix + first_tail.result()
    second_events, third_events = second_turn.result(), third_tail.result()
    assert first_events == second_events
    assert third_events == [event for event in first_events if event.seq > snapshot.last_seq]
    assert _deltas(first_events) == _text(message) + _deltas(third_events) == SLOW_TEXT
    assert isinstance(first_events[-1], TurnEnded) and first_events[-1].data.status == "done"
    view = await _view(live_client, session_url, idle=True)
    assert _text(view.messages[-1]) == SLOW_TEXT
    updates = [event.data.message for event in third_events if isinstance(event, MessageUpdated)]
    assert updates == [view.messages[-1]]


async def test_reconnect_with_last_event_id_continues_text_exactly_once(
    live_client: httpx2.AsyncClient, fake_openai: RunningFake, session_url: str
) -> None:
    await _assign_model(live_client, fake_openai, "fake-slow")
    async with _stream(live_client, session_url) as stream:
        await _send(live_client, session_url, "Reply slowly")
        prefix = await _through_delta(stream)
    assert (await _view(live_client, session_url)).busy
    async with _stream(live_client, session_url, after=prefix[-1].seq, header=True) as stream:
        suffix = await _until_end(stream)

    events = prefix + suffix
    seqs = [event.seq for event in events]
    assert seqs == sorted(set(seqs))
    assert suffix[0].seq > prefix[-1].seq
    assert not any(isinstance(event, Resync) for event in suffix)
    assert _deltas(events) == SLOW_TEXT
    ended = events[-1]
    assert isinstance(ended, TurnEnded) and ended.data.status == "done"
    view = await _view(live_client, session_url, idle=True)
    assert _text(view.messages[-1]) == _deltas(events)
    updates = [event.data.message for event in events if isinstance(event, MessageUpdated)]
    assert updates == [view.messages[-1]]
    assert view.last_seq == ended.seq


async def test_stop_preserves_partial_output_and_next_message_completes(
    live_client: httpx2.AsyncClient, fake_openai: RunningFake, session_url: str
) -> None:
    await _assign_model(live_client, fake_openai, "fake-slow")
    async with _stream(live_client, session_url) as stream:
        await _send(live_client, session_url, "Reply slowly")
        prefix = await _through_delta(stream)
        response = await live_client.post(session_url + "/stop")
        assert response.status_code == 204
        stopped_events = prefix + await _until_end(stream)
        ended = stopped_events[-1]
        assert isinstance(ended, TurnEnded) and ended.data.status == "stopped"
        stopped = (await _view(live_client, session_url, idle=True)).messages[-1]
        assert stopped.status == "stopped"
        assert _text(stopped) == _deltas(stopped_events)
        assert 0 < len(_text(stopped)) < len(SLOW_TEXT)
        sent = await _send(live_client, session_url, "Try again")
        next_events = await _until_end(stream)

    ended = next_events[-1]
    assert isinstance(ended, TurnEnded) and ended.data.status == "done"
    view = await _view(live_client, session_url, idle=True)
    assert view.busy is False
    assert len(view.messages) == 4
    assert [message.author_kind for message in view.messages] == ["user", "agent", "user", "agent"]
    assert view.messages[1:3] == [stopped, sent]
    assert view.messages[-1].status == "complete"
    assert _text(view.messages[-1]) == _deltas(next_events) == SLOW_TEXT


async def test_delete_project_during_turn_closes_stream_and_removes_project_folder(
    live_client: httpx2.AsyncClient,
    fake_openai: RunningFake,
    session_url: str,
    tmp_path: Path,
) -> None:
    await _assign_model(live_client, fake_openai, "fake-slow")
    project_url = session_url.split("/agents/")[0]
    project_id = project_url.rsplit("/", 1)[1]
    app_info = await live_client.get("/api/app")
    assert app_info.status_code == 200
    paths = DataPaths(Path(app_info.json()["data_dir"]))
    project_dir = paths.project_dir(project_id)
    async with _stream(live_client, session_url) as stream:
        await _send(live_client, session_url, "Reply slowly")
        await _through_delta(stream)
        assert (await _view(live_client, session_url)).busy
        assert project_dir.is_dir()
        async with asyncio.timeout(10):
            response = await live_client.delete(project_url)
            assert response.status_code == 204, response.text
            async for _ in stream:
                pass

    assert not project_dir.exists()
    assert (await live_client.get(project_url)).status_code == 404
    assert (tmp_path / "repo" / "README.md").is_file()
