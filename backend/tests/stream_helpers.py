"""Reading a session's event stream, shared by the runtime and the API tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from mlops_agents.agents.runtime.contract import StreamEvent, TextDelta, TurnEnded


async def through_first_delta(events: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    """Read up to the first text delta: with the fake's gate held, the turn is then held there.

    A turn that ends, or a stream that closes, before any text fails here, and the bounded wait
    fails a stream that stays silent, instead of leaving the test waiting.
    """
    received: list[StreamEvent] = []
    async with asyncio.timeout(10):
        async for event in events:
            received.append(event)
            assert not isinstance(event, TurnEnded), received
            if isinstance(event, TextDelta) and event.data.text:
                return received
    raise AssertionError(f"the stream closed before any text: {received}")
