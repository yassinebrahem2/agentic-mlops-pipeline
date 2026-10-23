"""Session routes and SSE stream (I7)."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette import EventSourceResponse, ServerSentEvent

from mlops_agents.agents.runtime.contract import (
    MessageView,
    SessionView,
    StreamEvent,
)
from mlops_agents.api.deps import ProjectDep, ServicesDep

router = APIRouter(tags=["sessions"])

PING_SECONDS: float = 15.0


def _parse_after_seq(last_event_id: str | None, after: str | None) -> int | None:
    raw = last_event_id if last_event_id is not None else after
    if raw is None:
        return None
    try:
        val = int(raw)
        return val if val >= 0 else None
    except (ValueError, TypeError):
        return None


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


class _EventSourceResponse(EventSourceResponse):
    media_type = "text/event-stream"


@router.get(
    "/projects/{project_id}/agents/{agent}/session/stream",
    response_class=_EventSourceResponse,
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
) -> _EventSourceResponse:
    sessions = services.sessions
    # Check agent and project before returning EventSourceResponse
    await sessions.get(project_id, agent)

    after_seq = _parse_after_seq(last_event_id, after)

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
            aclose = getattr(subscription, "aclose", None)
            if callable(aclose):
                close_result = aclose()
                if inspect.isawaitable(close_result):
                    await close_result

    return _EventSourceResponse(
        event_generator(),
        ping=PING_SECONDS,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
