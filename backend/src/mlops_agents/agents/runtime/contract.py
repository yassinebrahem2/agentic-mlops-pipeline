"""Session contract and types (I1).

SessionManager's constructor and lifecycle (implemented in task 5):

    SessionManager(
        *,
        databases: Databases,
        workspace: WorkspaceService,
        catalog: AgentCatalog,
        tools: ToolRegistry,
        settings: AgentSettingsService,
        providers: ProvidersService,
        http: httpx2.AsyncClient,
        git: GitRunner,
    )
    async def close(self) -> None  # cancels running turns and marks them interrupted

api-sessions builds it in the lifespan's AsyncExitStack and registers close.
"""

from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, Field

AuthorKind = Literal["user", "agent", "system"]
MessageStatus = Literal["complete", "streaming", "queued", "stopped", "interrupted"]
TurnEnd = Literal["done", "stopped", "interrupted", "failed"]


class TextPart(BaseModel):
    type: Literal["text"]
    text: str


class ToolCallPart(BaseModel):
    type: Literal["tool_call"]
    id: str
    name: str
    input: dict[str, Any]


class ToolResultPart(BaseModel):
    type: Literal["tool_result"]
    tool_call_id: str
    content: str
    is_error: bool


Part = Annotated[TextPart | ToolCallPart | ToolResultPart, Field(discriminator="type")]


class MessageView(BaseModel):
    id: str
    turn_id: str | None
    author_kind: AuthorKind
    author_name: str
    from_agent: str | None
    parts: list[Part]
    status: MessageStatus
    created_at: str


class SessionView(BaseModel):
    id: str
    agent: str
    busy: bool  # a turn is running or queued
    messages: list[MessageView]  # full history, oldest first
    last_seq: int  # stream position this view matches


class StreamEvent(BaseModel):
    seq: int  # per session, increasing; sent as the SSE id
    type: Literal[
        "message.created",
        "text.delta",
        "tool.started",
        "tool.finished",
        "message.updated",
        "turn.ended",
        "resync",
    ]
    data: dict[str, Any]


class AgentSessions(Protocol):
    async def get(self, project_id: str, agent: str) -> SessionView: ...

    async def send(self, project_id: str, agent: str, text: str) -> MessageView: ...

    async def stop(self, project_id: str, agent: str) -> None: ...

    def subscribe(
        self, project_id: str, agent: str, after_seq: int | None
    ) -> AsyncIterator[StreamEvent]: ...

    async def close_project(self, project_id: str) -> None:
        """Before a project is removed: ends its running turns (interrupted), drops its hubs and
        subscribers, closes its database handles; later calls for that project start fresh."""
        ...


__all__ = [
    "AgentSessions",
    "AuthorKind",
    "MessageStatus",
    "MessageView",
    "Part",
    "SessionView",
    "StreamEvent",
    "TextPart",
    "ToolCallPart",
    "ToolResultPart",
    "TurnEnd",
]
