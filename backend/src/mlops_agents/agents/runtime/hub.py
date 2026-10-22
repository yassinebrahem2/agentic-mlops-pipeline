from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncGenerator
from typing import Any

from mlops_agents.agents.runtime.contract import (
    MessageCreated,
    MessageData,
    MessageUpdated,
    MessageView,
    Resync,
    ResyncData,
    StreamEvent,
    TextDelta,
    TextDeltaData,
    TextPart,
    ToolCallPart,
    ToolFinished,
    ToolFinishedData,
    ToolResultPart,
    ToolStarted,
    ToolStartedData,
    TurnEnd,
    TurnEnded,
    TurnEndedData,
)

__all__ = ["AsyncRLock", "SessionHub"]

_RING_BUFFER_SIZE = 5000
_SLOW_SUBSCRIBER_THRESHOLD = 10000


class AsyncRLock:
    """Re-entrant asyncio lock for coordinating sessions."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._count = 0

    async def acquire(self) -> None:
        current = asyncio.current_task()
        if self._owner == current:
            self._count += 1
            return
        await self._lock.acquire()
        self._owner = current
        self._count = 1

    async def release(self) -> None:
        current = asyncio.current_task()
        if self._owner != current:
            raise RuntimeError("Cannot release un-acquired lock")
        self._count -= 1
        if self._count == 0:
            self._owner = None
            self._lock.release()

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.release()


class SessionHub:
    """In-memory event hub and turn sink for a single session."""

    def __init__(
        self,
        session_id: str,
        *,
        lock: AsyncRLock | None = None,
        initial_seq: int | None = None,
    ) -> None:
        self._session_id = session_id
        self._lock = lock if lock is not None else AsyncRLock()
        self._last_seq: int = initial_seq if initial_seq is not None else int(time.time() * 1000)
        self._buffer: deque[StreamEvent] = deque(maxlen=_RING_BUFFER_SIZE)
        self._subscribers: set[asyncio.Queue[StreamEvent | None]] = set()
        self._live_message: MessageView | None = None
        self._closed = False
        self._evicted_any = False

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def last_seq(self) -> int:
        return self._last_seq

    @property
    def current_seq(self) -> int:
        return self._last_seq

    @property
    def live_message(self) -> MessageView | None:
        return self._live_message

    def _next_seq(self) -> int:
        self._last_seq += 1
        return self._last_seq

    def _fold_event(self, event: StreamEvent) -> None:
        if isinstance(event, MessageCreated):
            if event.data.message.author_kind == "agent":
                self._live_message = event.data.message.model_copy(deep=True)
        elif isinstance(event, ToolStarted):
            if self._live_message is not None and self._live_message.id == event.data.message_id:
                self._live_message.parts.append(
                    ToolCallPart(
                        type="tool_call",
                        id=event.data.tool_call_id,
                        name=event.data.name,
                        input=event.data.input,
                    )
                )
        elif isinstance(event, ToolFinished):
            if self._live_message is not None and self._live_message.id == event.data.message_id:
                self._live_message.parts.append(
                    ToolResultPart(
                        type="tool_result",
                        tool_call_id=event.data.tool_call_id,
                        content=event.data.content,
                        is_error=event.data.is_error,
                    )
                )
        elif isinstance(event, TextDelta):
            if self._live_message is not None and self._live_message.id == event.data.message_id:
                if self._live_message.parts and isinstance(self._live_message.parts[-1], TextPart):
                    prev = self._live_message.parts[-1]
                    self._live_message.parts[-1] = TextPart(
                        type="text", text=prev.text + event.data.text
                    )
                else:
                    self._live_message.parts.append(TextPart(type="text", text=event.data.text))
        elif (
            isinstance(event, MessageUpdated)
            and self._live_message is not None
            and self._live_message.id == event.data.message.id
        ):
            self._live_message = event.data.message.model_copy(deep=True)

    def _publish_raw(self, event: StreamEvent) -> None:
        if len(self._buffer) == _RING_BUFFER_SIZE:
            self._evicted_any = True
        self._last_seq = max(self._last_seq, event.seq)
        self._fold_event(event)
        self._buffer.append(event)

        for q in list(self._subscribers):
            if q.qsize() >= _SLOW_SUBSCRIBER_THRESHOLD:
                while not q.empty():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                q.put_nowait(Resync(seq=self._last_seq, type="resync", data=ResyncData()))
            else:
                q.put_nowait(event)

    async def publish(self, event: StreamEvent) -> None:
        async with self._lock:
            self._publish_raw(event)

    async def text(self, message_id: str, text: str) -> None:
        async with self._lock:
            ev = TextDelta(
                seq=self._next_seq(),
                type="text.delta",
                data=TextDeltaData(message_id=message_id, text=text),
            )
            self._publish_raw(ev)

    async def tool_started(self, message_id: str, call: ToolCallPart) -> None:
        async with self._lock:
            ev = ToolStarted(
                seq=self._next_seq(),
                type="tool.started",
                data=ToolStartedData(
                    message_id=message_id,
                    tool_call_id=call.id,
                    name=call.name,
                    input=call.input,
                ),
            )
            self._publish_raw(ev)

    async def tool_finished(self, message_id: str, result: ToolResultPart) -> None:
        async with self._lock:
            ev = ToolFinished(
                seq=self._next_seq(),
                type="tool.finished",
                data=ToolFinishedData(
                    message_id=message_id,
                    tool_call_id=result.tool_call_id,
                    content=result.content,
                    is_error=result.is_error,
                ),
            )
            self._publish_raw(ev)

    async def message(self, message: MessageView, *, created: bool) -> None:
        async with self._lock:
            if created:
                ev = MessageCreated(
                    seq=self._next_seq(),
                    type="message.created",
                    data=MessageData(message=message),
                )
            else:
                ev = MessageUpdated(
                    seq=self._next_seq(),
                    type="message.updated",
                    data=MessageData(message=message),
                )
            self._publish_raw(ev)

    async def turn_ended(self, turn_id: str, status: TurnEnd, error: str | None = None) -> None:
        async with self._lock:
            ev = TurnEnded(
                seq=self._next_seq(),
                type="turn.ended",
                data=TurnEndedData(turn_id=turn_id, status=status, error=error),
            )
            self._publish_raw(ev)

    async def subscribe(self, after_seq: int | None = None) -> AsyncGenerator[StreamEvent, None]:
        if self._closed:
            return

        queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        self._subscribers.add(queue)

        try:
            current_seq = self._last_seq
            if not self._buffer:
                needs_resync = after_seq != current_seq
                replay: list[StreamEvent] = []
            elif (
                after_seq is None
                or after_seq > current_seq
                or (self._buffer and after_seq < self._buffer[0].seq - 1)
            ):
                needs_resync = True
                replay = []
            else:
                needs_resync = False
                replay = [e for e in self._buffer if e.seq > after_seq]

            if needs_resync:
                resync_ev = Resync(seq=current_seq, type="resync", data=ResyncData())
                yield resync_ev
                last_yielded = current_seq
            else:
                last_yielded = after_seq
                for ev in replay:
                    yield ev
                    last_yielded = ev.seq

            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, Resync):
                    yield item
                    last_yielded = item.seq
                    continue
                if last_yielded is not None and item.seq <= last_yielded:
                    continue
                yield item
                last_yielded = item.seq
        finally:
            self._subscribers.discard(queue)

    def close(self) -> None:
        self._closed = True
        for q in list(self._subscribers):
            q.put_nowait(None)
        self._subscribers.clear()
