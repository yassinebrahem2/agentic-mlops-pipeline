"""Session contract and types (I1).

SessionManager's constructor and lifecycle:

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
        observers: Sequence[TurnObserver] = (),  # told when each turn starts and ends
        bus: MessageBus | None = None,  # the manager binds itself to it once
    )
    async def close(self) -> None  # cancels running turns and marks them interrupted

api-sessions builds it in the lifespan's AsyncExitStack and registers close.
"""

from collections.abc import AsyncGenerator, Callable
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.tools.base import Preview, TaskStatus

AuthorKind = Literal["user", "agent", "system"]
MessageStatus = Literal["complete", "streaming", "queued", "stopped", "interrupted", "waiting"]
ApprovalStatus = Literal["pending", "approved", "rejected", "cancelled", "expired"]
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


class ApprovalPart(BaseModel):
    type: Literal["approval"]
    approval_id: str
    tool_call_id: str
    preview: Preview
    status: ApprovalStatus
    note: str | None


Part = Annotated[
    TextPart | ToolCallPart | ToolResultPart | ApprovalPart, Field(discriminator="type")
]


class MessageView(BaseModel):
    id: str
    turn_id: str | None
    author_kind: AuthorKind
    author_name: str
    from_agent: str | None
    parts: list[Part]
    status: MessageStatus
    created_at: str


class TaskView(BaseModel):
    id: str
    agent: str
    title: str
    branch: str
    status: TaskStatus
    review_kind: Literal["github", "local"] | None
    pr_number: int | None
    pr_url: str | None


class SessionView(BaseModel):
    id: str
    agent: str
    busy: bool  # a turn is running or queued
    messages: list[MessageView]  # full history, oldest first
    last_seq: int  # stream position this view matches
    current_task: TaskView | None


class MessageData(BaseModel):
    message: MessageView


class TextDeltaData(BaseModel):
    message_id: str
    text: str


class ToolStartedData(BaseModel):
    message_id: str
    tool_call_id: str
    name: str
    input: dict[str, Any]


class ToolFinishedData(BaseModel):
    message_id: str
    tool_call_id: str
    content: str
    is_error: bool


class TurnEndedData(BaseModel):
    turn_id: str
    status: TurnEnd
    error: str | None


class ResyncData(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TurnWaitingData(BaseModel):
    turn_id: str


class _Event(BaseModel):
    seq: int  # per session, increasing; sent as the SSE id


class MessageCreated(_Event):
    """Every new message, the user's included."""

    type: Literal["message.created"]
    data: MessageData


class TextDelta(_Event):
    type: Literal["text.delta"]
    data: TextDeltaData


class ToolStarted(_Event):
    type: Literal["tool.started"]
    data: ToolStartedData


class ToolFinished(_Event):
    type: Literal["tool.finished"]
    data: ToolFinishedData


class MessageUpdated(_Event):
    """A message's new final state."""

    type: Literal["message.updated"]
    data: MessageData


class TurnWaiting(_Event):
    """The turn is paused for an approval. An approval part changes through message.updated."""

    type: Literal["turn.waiting"]
    data: TurnWaitingData


class TurnEnded(_Event):
    type: Literal["turn.ended"]
    data: TurnEndedData


class Resync(_Event):
    """The client's position is unknown to the hub: it calls get again."""

    type: Literal["resync"]
    data: ResyncData


# The one definition of the stream's events; the frontend's types are generated from it (#89).
StreamEvent = Annotated[
    MessageCreated
    | TextDelta
    | ToolStarted
    | ToolFinished
    | MessageUpdated
    | TurnWaiting
    | TurnEnded
    | Resync,
    Field(discriminator="type"),
]


class AgentSessions(Protocol):
    async def get(self, project_id: str, agent: str) -> SessionView: ...

    async def send(self, project_id: str, agent: str, text: str) -> MessageView: ...

    async def post(
        self,
        project_id: str,
        agent: str,
        *,
        author_name: str,
        from_agent: str,
        text: str,
        exchange_id: str | None,
        hop: int | None,
    ) -> MessageView:
        """An inbound message from another agent, or a wake-up (author_name "Scheduled wake-up",
        from_agent = the agent itself). Stored as author_kind "user" with a queued turn, like send."""
        ...

    async def turn_states(self, project_id: str) -> dict[str, Literal["running", "waiting"]]:
        """Each agent whose session has a running or waiting turn now."""
        ...

    async def update_message(
        self,
        project_id: str,
        session_id: str,
        message_id: str,
        transform: Callable[[list[Part]], list[Part]],
    ) -> MessageView:
        """Reads the stored parts, applies transform, stores and publishes message.updated, all
        under the session lock, so two concurrent updates (two approvals decided at once) both
        land."""
        ...

    async def stop(self, project_id: str, agent: str) -> None: ...

    async def ensure_recovered(self, project_id: str) -> None: ...

    async def resume_turn(self, project_id: str, session_id: str, turn_id: str) -> None: ...

    def subscribe(
        self, project_id: str, agent: str, after_seq: int | None
    ) -> AsyncGenerator[StreamEvent, None]: ...

    async def close_project(self, project_id: str) -> None:
        """Before a project is removed: ends its running turns (interrupted), drops its hubs and
        subscribers, closes its database handles; later calls for that project start fresh."""
        ...


__all__ = [
    "AgentSessions",
    "ApprovalPart",
    "ApprovalStatus",
    "AuthorKind",
    "MessageCreated",
    "MessageData",
    "MessageStatus",
    "MessageUpdated",
    "MessageView",
    "Part",
    "Resync",
    "ResyncData",
    "SessionView",
    "StreamEvent",
    "TaskView",
    "TextDelta",
    "TextDeltaData",
    "TextPart",
    "ToolCallPart",
    "ToolFinished",
    "ToolFinishedData",
    "ToolResultPart",
    "ToolStarted",
    "ToolStartedData",
    "TurnEnd",
    "TurnEnded",
    "TurnEndedData",
    "TurnWaiting",
    "TurnWaitingData",
]
