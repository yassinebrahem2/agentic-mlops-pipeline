from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal

from mlops_agents.agents.runtime.contract import (
    AgentSessions,
    MessageView,
    Part,
    SessionView,
    StreamEvent,
    TextPart,
)
from mlops_agents.clock import now_iso
from mlops_agents.errors import DomainError
from mlops_agents.ids import new_id


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


class FakeSubscription:
    """Scripted events first, then live events from a queue; records when it is closed."""

    def __init__(self, events: list[StreamEvent], queue: asyncio.Queue[StreamEvent]) -> None:
        self.events = list(events)
        self.queue = queue
        self.closed = False
        self.closed_event = asyncio.Event()

    async def stream(self) -> AsyncGenerator[StreamEvent, None]:
        try:
            for event in self.events:
                yield event
            while True:
                yield await self.queue.get()
        finally:
            self.closed = True
            self.closed_event.set()


class FakeSessions(AgentSessions):
    """In-memory test double for AgentSessions."""

    def __init__(
        self,
        *,
        default_view: SessionView | None = None,
        scripted_events: list[StreamEvent] | None = None,
        unknown_agents: set[str] | None = None,
        no_chat_agents: set[str] | None = None,
        order_log: list[str] | None = None,
    ) -> None:
        self.views: dict[tuple[str, str], SessionView] = {}
        self.default_view = default_view
        self.scripted_events = scripted_events or []
        self.unknown_agents = unknown_agents or set()
        self.no_chat_agents = no_chat_agents or set()
        self.order_log = order_log if order_log is not None else []
        self.stopped: list[tuple[str, str]] = []
        self.closed_projects: list[str] = []
        self.sent_messages: list[tuple[str, str, str]] = []
        self.posted_messages: list[tuple[str, str, str, str]] = []
        self.subscriptions: list[FakeSubscription] = []
        self.subscribe_calls: list[tuple[str, str, int | None]] = []
        self.live_queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
        self.subscribed = asyncio.Event()

    def _check_agent(self, agent: str) -> None:
        if agent in self.unknown_agents:
            raise DomainError("agent_not_found", f"There is no agent named {agent}.", status=404)
        if agent in self.no_chat_agents:
            raise DomainError(
                "agent_has_no_chat", f"Agent {agent} does not support chat.", status=409
            )

    async def get(self, project_id: str, agent: str) -> SessionView:
        self._check_agent(agent)
        key = (project_id, agent)
        if key in self.views:
            return self.views[key]
        if self.default_view is not None:
            return self.default_view
        return SessionView(
            id=f"ses_{agent}",
            agent=agent,
            busy=False,
            messages=[],
            last_seq=0,
            current_task=None,
        )

    async def send(self, project_id: str, agent: str, text: str) -> MessageView:
        self._check_agent(agent)
        self.sent_messages.append((project_id, agent, text))
        return await self._append(project_id, agent, text, author_name="User", from_agent=None)

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
        self._check_agent(agent)
        self.posted_messages.append((project_id, agent, from_agent, text))
        return await self._append(
            project_id, agent, text, author_name=author_name, from_agent=from_agent
        )

    async def _append(
        self, project_id: str, agent: str, text: str, *, author_name: str, from_agent: str | None
    ) -> MessageView:
        msg = MessageView(
            id=new_id("msg"),
            turn_id=new_id("trn"),
            author_kind="user",
            author_name=author_name,
            from_agent=from_agent,
            parts=[TextPart(type="text", text=text)],
            status="complete",
            created_at=now_iso(),
        )
        view = await self.get(project_id, agent)
        self.views[(project_id, agent)] = SessionView(
            id=view.id,
            agent=view.agent,
            busy=view.busy,
            messages=[*view.messages, msg],
            last_seq=view.last_seq,
            current_task=view.current_task,
        )
        return msg

    async def update_message(
        self,
        project_id: str,
        session_id: str,
        message_id: str,
        transform: Callable[[list[Part]], list[Part]],
    ) -> MessageView:
        for (view_project, agent), view in self.views.items():
            if view_project != project_id or view.id != session_id:
                continue
            for i, message in enumerate(view.messages):
                if message.id == message_id:
                    updated = message.model_copy(update={"parts": transform(list(message.parts))})
                    messages = [*view.messages[:i], updated, *view.messages[i + 1 :]]
                    self.views[(project_id, agent)] = view.model_copy(update={"messages": messages})
                    return updated
        raise DomainError("message_not_found", "This message does not exist.", status=404)

    async def stop(self, project_id: str, agent: str) -> None:
        self._check_agent(agent)
        self.stopped.append((project_id, agent))

    def subscribe(
        self, project_id: str, agent: str, after_seq: int | None
    ) -> AsyncGenerator[StreamEvent, None]:
        self._check_agent(agent)
        self.subscribe_calls.append((project_id, agent, after_seq))
        sub = FakeSubscription(self.scripted_events.copy(), self.live_queue)
        self.subscriptions.append(sub)
        self.subscribed.set()
        return sub.stream()

    async def turn_states(self, project_id: str) -> dict[str, Literal["running", "waiting"]]:
        return {}

    async def close_project(self, project_id: str) -> None:
        self.closed_projects.append(project_id)
        self.order_log.append(f"close_project:{project_id}")

    async def ensure_recovered(self, project_id: str) -> None:
        pass

    async def resume_turn(self, project_id: str, session_id: str, turn_id: str) -> None:
        pass
