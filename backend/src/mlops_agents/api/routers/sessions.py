"""Session routes and SSE stream (I7)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette import ServerSentEvent

from mlops_agents.agents.runtime.contract import (
    MessageView,
    SessionView,
    StreamEvent,
)
from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.api.sse import PING_SECONDS, SSE_HEADERS, SseResponse, parse_after_seq

router = APIRouter(tags=["sessions"])


class SendMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)


@router.get(
    "/projects/{project_id}/agents/{agent}/session",
    response_model=SessionView,
)
async def get_session(
    project_id: str,
    agent: str,
    services: ServicesDep,
    _project: ProjectDep,
) -> SessionView:
    return await services.sessions.get(project_id, agent)


@router.post(
    "/projects/{project_id}/agents/{agent}/session/messages",
    response_model=MessageView,
    status_code=status.HTTP_201_CREATED,
)
async def send_message(
    project_id: str,
    agent: str,
    body: SendMessage,
    services: ServicesDep,
    _project: ProjectDep,
) -> MessageView:
    return await services.sessions.send(project_id, agent, body.text)


@router.post(
    "/projects/{project_id}/agents/{agent}/session/stop",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def stop_session(
    project_id: str,
    agent: str,
    services: ServicesDep,
    _project: ProjectDep,
) -> Response:
    await services.sessions.stop(project_id, agent)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/projects/{project_id}/agents/{agent}/session/compact",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def compact_session(
    project_id: str,
    agent: str,
    services: ServicesDep,
    _project: ProjectDep,
) -> Response:
    await services.sessions.compact(project_id, agent)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/projects/{project_id}/agents/{agent}/session/clear",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def clear_session(
    project_id: str,
    agent: str,
    services: ServicesDep,
    _project: ProjectDep,
) -> Response:
    await services.sessions.clear(project_id, agent)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/projects/{project_id}/agents/{agent}/session/stream",
    response_class=SseResponse,
    responses={
        200: {
            "model": StreamEvent,
            "description": "I1 stream events",
            "content": {"text/event-stream": {"schema": {"type": "object"}}},
        }
    },
)
async def stream_session(
    project_id: str,
    agent: str,
    services: ServicesDep,
    _project: ProjectDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    after: Annotated[str | None, Query()] = None,
) -> SseResponse:
    sessions = services.sessions
    # Errors cannot be sent once the stream's headers are out, and subscribe only raises on
    # its first event, so an unknown or chat-less agent must fail here as a JSON error.
    await sessions.get(project_id, agent)

    after_seq = parse_after_seq(last_event_id, after)

    async def event_generator() -> AsyncIterator[ServerSentEvent]:
        subscription = sessions.subscribe(project_id, agent, after_seq=after_seq)
        try:
            async for event in subscription:
                yield ServerSentEvent(
                    id=str(event.seq),
                    event=event.type,
                    data=event.data.model_dump_json(),
                )
        finally:
            await subscription.aclose()

    return SseResponse(
        event_generator(),
        ping=PING_SECONDS,
        headers=dict(SSE_HEADERS),
    )
