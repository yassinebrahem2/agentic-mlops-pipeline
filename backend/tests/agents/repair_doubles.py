"""The doubles of the restart repair's tests: a process dying at a chosen write (an observer,
a log or a call that raises CancelledError, as a process dying there would leave it), an observer
held until released, and a turn observer counting each turn's waits and ends."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from mlops_agents.agents.approvals.contract import ApprovalView
from mlops_agents.agents.policy.table import PermissionLevel
from mlops_agents.agents.runtime.contract import TurnEnd
from mlops_agents.agents.runtime.hooks import TurnObserver
from mlops_agents.agents.tools.base import RiskClass
from mlops_agents.events.audit import SqliteAuditLog
from mlops_agents.events.contract import AuditDecision, AuditRecord
from mlops_agents.storage.databases import Databases


class CrashOnDecided:
    """The process dies while the observers are told of a decision: the first `decided` raises
    CancelledError (a plain Exception would be caught and logged, and the decision go on)."""

    async def asked(self, view: ApprovalView) -> None:
        return None

    async def decided(self, view: ApprovalView) -> None:
        raise asyncio.CancelledError


class TurnCounter(TurnObserver):
    """Records each turn's waits and ends, as (turn ID, status)."""

    def __init__(self) -> None:
        self.waited: list[str] = []
        self.ended: list[tuple[str, str]] = []
        self._changed = asyncio.Event()

    async def turn_started(self, project_id: str, agent: str, turn_id: str) -> None:
        return None

    async def turn_waiting(self, project_id: str, agent: str, turn_id: str) -> None:
        self.waited.append(turn_id)
        self._changed.set()

    async def turn_ended(
        self, project_id: str, agent: str, turn_id: str, status: TurnEnd, final_text: str | None
    ) -> None:
        self.ended.append((turn_id, status))
        self._changed.set()

    async def wait_ended(self, turn_id: str) -> None:
        """Until the turn's end is reported to the observers (after its stream event)."""
        async with asyncio.timeout(10):
            while all(t != turn_id for t, _ in self.ended):
                self._changed.clear()
                await self._changed.wait()


class CancelledOnceAuditLog(SqliteAuditLog):
    """Its first write is cancelled, as a process dying there would leave it."""

    def __init__(self, databases: Databases) -> None:
        super().__init__(databases, secrets=[])
        self.calls = 0

    async def record(
        self,
        project_id: str,
        *,
        actor: str,
        action: str,
        risk_class: RiskClass,
        permission_level: PermissionLevel,
        decision: AuditDecision,
        target: str,
        detail: dict[str, Any],
    ) -> AuditRecord:
        self.calls += 1
        if self.calls == 1:
            raise asyncio.CancelledError
        return await super().record(
            project_id,
            actor=actor,
            action=action,
            risk_class=risk_class,
            permission_level=permission_level,
            decision=decision,
            target=target,
            detail=detail,
        )


def cancelled_once(call: Callable[..., Awaitable[object]]) -> Callable[..., Awaitable[object]]:
    """`call`, whose first call is cancelled, as a process dying there would leave it."""
    calls = 0

    async def wrapped(*args: Any, **kwargs: Any) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError
        return await call(*args, **kwargs)

    return wrapped


class HeldObserver:
    """Holds `decided` for each listed approval until its release, as a slow proposal `apply`
    would; records which calls finished and which were cancelled."""

    def __init__(self, approval_ids: Iterable[str]) -> None:
        ids = list(approval_ids)
        self.entered = {i: asyncio.Event() for i in ids}
        self.release = {i: asyncio.Event() for i in ids}
        self.finished: list[str] = []
        self.cancelled: list[str] = []

    async def asked(self, view: ApprovalView) -> None:
        return None

    async def decided(self, view: ApprovalView) -> None:
        if view.id not in self.entered:
            return
        self.entered[view.id].set()
        try:
            await self.release[view.id].wait()
        except asyncio.CancelledError:
            self.cancelled.append(view.id)
            raise
        self.finished.append(view.id)

    async def wait_entered(self, approval_id: str) -> None:
        await asyncio.wait_for(self.entered[approval_id].wait(), timeout=10)
