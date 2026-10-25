"""Project events router and SSE stream."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Header, Query
from sse_starlette import ServerSentEvent

from mlops_agents.agents.runtime.contract import Resync
from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.api.sse import PING_SECONDS, SSE_HEADERS, SseResponse, parse_after_seq
from mlops_agents.events.contract import AgentStatus, EventRecord

router = APIRouter(tags=["events"])


@router.get(
    "/projects/{project_id}/events/stream",
    response_class=SseResponse,
    responses={
        200: {
            "model": EventRecord | Resync,
            "description": "Project event stream",
            "content": {"text/event-stream": {"schema": {"type": "object"}}},
        }
    },
)
async def stream_events(
    project_id: str,
    services: ServicesDep,
    _project: ProjectDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    after: Annotated[str | None, Query()] = None,
) -> SseResponse:
    after_seq = parse_after_seq(last_event_id, after)

    async def event_generator() -> AsyncIterator[ServerSentEvent]:
        subscription = services.events.subscribe(project_id, after_seq=after_seq)
        try:
            async for event in subscription:
                if isinstance(event, Resync):
                    data = event.data.model_dump_json()
                else:
                    data = event.model_dump_json()
                yield ServerSentEvent(
                    id=str(event.seq),
                    event=event.type,
                    data=data,
                )
        finally:
            await subscription.aclose()

    return SseResponse(
        event_generator(),
        ping=PING_SECONDS,
        headers=dict(SSE_HEADERS),
    )


@router.get(
    "/projects/{project_id}/agents/status",
    response_model=list[AgentStatus],
)
async def get_agents_status(
    project_id: str,
    services: ServicesDep,
    _project: ProjectDep,
) -> list[AgentStatus]:
    return await services.status_board.snapshot(project_id)
