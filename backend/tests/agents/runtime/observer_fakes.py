"""Turn observers and helpers the session observer tests share."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from mlops_agents.agents.runtime.contract import ErrorKind, TurnEnd
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import turns


class RecordingObserver:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.ended: asyncio.Queue[TurnEnd] = asyncio.Queue()
        self._fail = fail

    async def turn_started(self, project_id: str, agent: str, turn_id: str) -> None:
        self.calls.append(("started", project_id, agent, turn_id))
        if self._fail:
            raise RuntimeError("observer failed")

    async def turn_waiting(self, project_id: str, agent: str, turn_id: str) -> None:
        self.calls.append(("waiting", project_id, agent, turn_id))

    async def turn_ended(
        self,
        project_id: str,
        agent: str,
        turn_id: str,
        status: TurnEnd,
        final_text: str | None,
        error_kind: ErrorKind | None,
    ) -> None:
        self.calls.append(("ended", project_id, agent, turn_id, status, str(final_text)))
        self.ended.put_nowait(status)
        if self._fail:
            raise RuntimeError("observer failed")


class HangingObserver(RecordingObserver):
    """Hangs in turn_started or turn_ended until the test lets it go (which it never does).

    The first `passes` calls of that hook return; the next one hangs.
    """

    def __init__(self, *, hang_in: str, passes: int = 0) -> None:
        super().__init__()
        self.hanging = asyncio.Event()
        self._hang_in = hang_in
        self._passes = passes

    async def _hang(self) -> None:
        if self._passes > 0:
            self._passes -= 1
            return
        self.hanging.set()
        await asyncio.Event().wait()

    async def turn_started(self, project_id: str, agent: str, turn_id: str) -> None:
        await super().turn_started(project_id, agent, turn_id)
        if self._hang_in == "started":
            await self._hang()

    async def turn_ended(
        self,
        project_id: str,
        agent: str,
        turn_id: str,
        status: TurnEnd,
        final_text: str | None,
        error_kind: ErrorKind | None,
    ) -> None:
        await super().turn_ended(project_id, agent, turn_id, status, final_text, error_kind)
        if self._hang_in == "ended":
            await self._hang()


class HeldObserver(RecordingObserver):
    """Holds the first turn_started until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def turn_started(self, project_id: str, agent: str, turn_id: str) -> None:
        await super().turn_started(project_id, agent, turn_id)
        if not self.entered.is_set():
            self.entered.set()
            await self.release.wait()


class HeldEndObserver(RecordingObserver):
    """Holds the first turn_ended until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def turn_ended(
        self,
        project_id: str,
        agent: str,
        turn_id: str,
        status: TurnEnd,
        final_text: str | None,
        error_kind: ErrorKind | None,
    ) -> None:
        await super().turn_ended(project_id, agent, turn_id, status, final_text, error_kind)
        if not self.entered.is_set():
            self.entered.set()
            await self.release.wait()


class WorkerCapturingObserver(RecordingObserver):
    """Keeps the task that calls turn_started: the session's worker."""

    def __init__(self) -> None:
        super().__init__()
        self.workers: list[asyncio.Task[object]] = []
        self.started = asyncio.Event()

    async def turn_started(self, project_id: str, agent: str, turn_id: str) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.workers.append(task)
        self.started.set()
        await super().turn_started(project_id, agent, turn_id)


class SelfCancellingObserver(RecordingObserver):
    """Raises its own CancelledError, as an observer awaiting a task someone else cancelled."""

    async def turn_started(self, project_id: str, agent: str, turn_id: str) -> None:
        await super().turn_started(project_id, agent, turn_id)
        raise asyncio.CancelledError

    async def turn_ended(
        self,
        project_id: str,
        agent: str,
        turn_id: str,
        status: TurnEnd,
        final_text: str | None,
        error_kind: ErrorKind | None,
    ) -> None:
        await super().turn_ended(project_id, agent, turn_id, status, final_text, error_kind)
        raise asyncio.CancelledError


async def turn_statuses(databases: Databases, project_id: str) -> list[str]:
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        return [str(s) for s in (await conn.execute(select(turns.c.status))).scalars()]
