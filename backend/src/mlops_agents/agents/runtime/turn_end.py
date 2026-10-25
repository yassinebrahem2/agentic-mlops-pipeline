"""A taken turn's end: its stored end state, then the report to every turn observer."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, replace

from mlops_agents.agents.runtime.contract import TextPart, TurnEnd
from mlops_agents.agents.runtime.executor import UNEXPECTED_FAILURE_TEXT, TurnExecutor
from mlops_agents.agents.runtime.hooks import TurnObserver
from mlops_agents.agents.runtime.hub import AsyncRLock, SessionHub
from mlops_agents.agents.runtime.shielded import shielded
from mlops_agents.agents.runtime.store import SessionStore

logger = logging.getLogger(__name__)

__all__ = ["OBSERVER_TIMEOUT_S", "UNEXPECTED_FAILURE", "Ending", "TurnEnds", "observed"]

# How long one observer call may take before it is abandoned and logged.
OBSERVER_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class Ending:
    """How a taken turn ended, decided before anything of it is stored. `failure_notice` is
    the system message of a failure still to be written: None when there is none left to
    write (no failure, written already, or recorded by the executor itself)."""

    status: TurnEnd
    error: str | None = None
    failure_notice: str | None = None

    @property
    def notice_recorded(self) -> bool:
        """Whether this is a failed end whose failure notice is stored."""
        return self.status == "failed" and self.failure_notice is None


# A failure no part of the turn recorded: its system message is written with the end.
UNEXPECTED_FAILURE = Ending("failed", UNEXPECTED_FAILURE_TEXT, UNEXPECTED_FAILURE_TEXT)


class TurnEnds:
    """The one owner of a taken turn's end, and of its trigger message's settling."""

    def __init__(
        self,
        *,
        store: SessionStore,
        executor: TurnExecutor,
        observers: Sequence[TurnObserver],
    ) -> None:
        self._store = store
        self._executor = executor
        self._observers = tuple(observers)

    @shielded
    async def end(
        self,
        project_id: str,
        session_id: str,
        agent: str,
        turn_id: str,
        trigger_msg_id: str,
        hub: SessionHub,
        lock: AsyncRLock,
        ending: Ending,
    ) -> None:
        """Store a taken turn's end state, then report it to every observer. It runs whole: a
        cancel of the worker (close, close_project, or both) waits for it and is raised after,
        so it never rewrites the end nor reports it twice.

        It runs in its own task and takes the session lock, which the worker never holds when
        it gets here, and the turn task has finished by then.
        """
        if ending.status == "interrupted":
            await self._store.set_turn(project_id, turn_id, "interrupted")
            await self._store.mark_interrupted(project_id, turn_id)
            # A cancel before the trigger was read would leave it queued for good.
            await self.settle_trigger(
                project_id, session_id, turn_id, trigger_msg_id, hub, lock, read=False
            )
            await hub.turn_ended(turn_id, status="interrupted")
        else:
            try:
                ending = await self._write_notice(
                    project_id, session_id, turn_id, hub, lock, ending
                )
                await self._store_end(project_id, turn_id, hub, ending)
            except Exception:
                # As any other unexpected failure of the turn: it ends failed and the queue goes
                # on, with one failure notice. A failed end whose notice is stored keeps its
                # error. A failure of this fallback too is raised.
                logger.exception("Could not store how turn %s ended; it ends failed", turn_id)
                if not ending.notice_recorded:
                    ending = await self._write_notice(
                        project_id, session_id, turn_id, hub, lock, UNEXPECTED_FAILURE
                    )
                await self._store_end(project_id, turn_id, hub, ending)
        await self._turn_ended(project_id, session_id, agent, turn_id, ending.status)

    async def settle_trigger(
        self,
        project_id: str,
        session_id: str,
        turn_id: str,
        trigger_msg_id: str,
        hub: SessionHub,
        lock: AsyncRLock,
        *,
        read: bool,
    ) -> None:
        """A queued trigger becomes complete. When the turn `read` it and it is from an agent,
        it records this turn read it; a turn interrupted before reading it records nothing."""
        async with lock:
            trigger = await self._store.message(project_id, session_id, trigger_msg_id)
            from_agent = trigger is not None and trigger.from_agent is not None
            read_by = turn_id if read and from_agent else None
            if trigger is not None and trigger.status == "queued":
                updated_msg = await self._store.update_message(
                    project_id, trigger_msg_id, status="complete", read_by_turn=read_by
                )
                await hub.message(updated_msg, created=False)
            elif read_by is not None:
                await self._store.update_message(project_id, trigger_msg_id, read_by_turn=read_by)

    async def _write_notice(
        self,
        project_id: str,
        session_id: str,
        turn_id: str,
        hub: SessionHub,
        lock: AsyncRLock,
        ending: Ending,
    ) -> Ending:
        """Write the ending's failure notice still to be written, if any; return it with none."""
        if ending.failure_notice is None:
            return ending
        await self._executor.record_failure(
            project_id, session_id, turn_id, hub, lock, ending.failure_notice
        )
        return replace(ending, failure_notice=None)

    async def _store_end(
        self, project_id: str, turn_id: str, hub: SessionHub, ending: Ending
    ) -> None:
        """Store a turn's decided end, other than interrupted, and publish it."""
        await self._store.set_turn(project_id, turn_id, ending.status, error=ending.error)
        await hub.turn_ended(turn_id, status=ending.status, error=ending.error)

    async def _turn_ended(
        self, project_id: str, session_id: str, agent: str, turn_id: str, status: TurnEnd
    ) -> None:
        """Every observer hears the end, one after another. It runs inside `end`'s shielded
        task, so a cancel of the worker waits for the last of them."""
        if not self._observers:
            return
        try:
            final_text = await self._final_text(project_id, session_id, turn_id)
        except Exception:
            # The turn has ended either way; the observers still hear of it.
            logger.exception("Could not read the final text of turn %s", turn_id)
            final_text = None
        for observer in self._observers:
            await observed(observer.turn_ended(project_id, agent, turn_id, status, final_text))

    async def _final_text(self, project_id: str, session_id: str, turn_id: str) -> str | None:
        """The last text the agent wrote in the turn, or None when it wrote none."""
        message = await self._store.turn_agent_message(project_id, session_id, turn_id)
        if message is None:
            return None
        texts = [part.text for part in message.parts if isinstance(part, TextPart) and part.text]
        return texts[-1] if texts else None


async def observed(call: Awaitable[None]) -> None:
    """Await one observer call, bounded. A failing or hanging observer is logged and never
    stops the queue. The end report runs the observers one after another through a cancel,
    so hanging ones hold a shutdown up to OBSERVER_TIMEOUT_S each."""
    try:
        async with asyncio.timeout(OBSERVER_TIMEOUT_S):
            await call
    except asyncio.CancelledError:
        # Only a cancel of the worker itself goes on; an observer's own cancel is its failure.
        current = asyncio.current_task()
        if current is not None and current.cancelling() > 0:
            raise
        logger.exception("A turn observer was cancelled")
    except Exception:
        logger.exception("A turn observer failed")
