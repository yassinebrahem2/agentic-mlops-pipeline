"""TurnObserver implementation recording turn lifecycle events to EventLog (I11)."""

from __future__ import annotations

from typing import Final

from mlops_agents.agents.runtime.contract import TurnEnd
from mlops_agents.agents.runtime.hooks import TurnObserver
from mlops_agents.events.contract import EventLog

__all__ = ["EventTurnObserver"]

_FINAL_TEXT_MAX_LEN: Final[int] = 200


class EventTurnObserver(TurnObserver):
    """Observes turn execution and records turn lifecycle events into the project's EventLog."""

    def __init__(self, events: EventLog) -> None:
        self._events = events

    async def turn_started(self, project_id: str, agent: str, turn_id: str) -> None:
        await self._events.record(
            project_id,
            source=agent,
            type="turn.started",
            subject_id=turn_id,
            payload={},
        )

    async def turn_waiting(self, project_id: str, agent: str, turn_id: str) -> None:
        await self._events.record(
            project_id,
            source=agent,
            type="turn.waiting",
            subject_id=turn_id,
            payload={},
        )

    async def turn_ended(
        self,
        project_id: str,
        agent: str,
        turn_id: str,
        status: TurnEnd,
        final_text: str | None,
    ) -> None:
        capped_text = final_text[:_FINAL_TEXT_MAX_LEN] if final_text is not None else None
        await self._events.record(
            project_id,
            source=agent,
            type="turn.ended",
            subject_id=turn_id,
            payload={"status": status, "final_text": capped_text},
        )
