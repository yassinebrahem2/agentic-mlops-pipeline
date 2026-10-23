from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing

import pytest

from mlops_agents.agents.runtime.contract import StreamEvent
from mlops_agents.agents.runtime.hub import SessionHub


def watch_hub_subscriptions(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """Set the returned event once a hub subscription's generator has finished closing (its
    `finally` is what drops the subscriber). The hub exposes no subscriber count, so this
    watches the closing itself. The relays are kept referenced so that only an explicit
    `aclose` can finish them, never garbage collection."""
    closed = asyncio.Event()
    held: list[AsyncGenerator[StreamEvent, None]] = []
    original = SessionHub.subscribe

    async def relay(inner: AsyncGenerator[StreamEvent, None]) -> AsyncGenerator[StreamEvent, None]:
        try:
            async with aclosing(inner):
                async for event in inner:
                    yield event
        finally:
            closed.set()

    def subscribe(
        self: SessionHub, after_seq: int | None = None
    ) -> AsyncGenerator[StreamEvent, None]:
        relayed = relay(original(self, after_seq))
        held.append(relayed)
        return relayed

    monkeypatch.setattr(SessionHub, "subscribe", subscribe)
    return closed
