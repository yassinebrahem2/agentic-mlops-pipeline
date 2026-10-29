"""Stop settles its cancels holding the session lock while a decision of the same session settles
and then rewrites its card under that lock: the settle lock never covers a card write, so the two
never wait on each other (#381)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2

from mlops_agents.agents.approvals.service import ApprovalsService
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.contract import ApprovalPart, Part
from mlops_agents.agents.tools.base import RiskClass
from mlops_agents.events.contract import EventRecord, EventType
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.approval_env import TOOL_PREVIEW, add_turn
from tests.agents.runtime.session_env import build_session_env
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


class FirstEventWaits(SqliteEventLog):
    """Its first write waits for `release`: the decision holds the settle lock meanwhile."""

    def __init__(self, databases: Databases) -> None:
        super().__init__(databases)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def record(
        self,
        project_id: str,
        *,
        source: str,
        type: EventType,
        payload: dict[str, Any],
        subject_id: str | None = None,
    ) -> EventRecord:
        if not self.entered.is_set():
            self.entered.set()
            await self.release.wait()
        return await super().record(
            project_id, source=source, type=type, payload=payload, subject_id=subject_id
        )


class ContendedSettleStore(ApprovalStore):
    """Sets `contended` when a caller asks for the settle lock while another is inside it."""

    def __init__(self, databases: Databases) -> None:
        super().__init__(databases)
        self.contended = asyncio.Event()
        self._entered = 0

    @asynccontextmanager
    async def settling(self, project_id: str) -> AsyncGenerator[None]:
        if self._entered:
            self.contended.set()
        self._entered += 1
        try:
            async with super().settling(project_id):
                yield
        finally:
            self._entered -= 1


async def test_a_stop_settling_while_a_decision_settles_does_not_deadlock(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    # The one store of the manager, its gate and the service.
    store = ContendedSettleStore(databases)
    log = FirstEventWaits(databases)
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        approval_store=store,
        event_log=log,
    )
    project_id = env.project_id
    session_id = await env.store.session_id(project_id, "orchestrator")
    ids = ["apr_decided", "apr_stopped"]
    parts: list[Part] = [
        ApprovalPart(
            type="approval",
            approval_id=i,
            tool_call_id=f"call_{i}",
            preview=TOOL_PREVIEW,
            status="pending",
            note=None,
        )
        for i in ids
    ]
    message_id = await add_turn(
        databases, project_id, session_id=session_id, turn_id="trn_race", parts=parts
    )
    for i in ids:
        await store.create(
            project_id,
            id=i,
            turn_id="trn_race",
            session_id=session_id,
            message_id=message_id,
            agent="orchestrator",
            tool_call_id=f"call_{i}",
            tool="edit_file",
            risk_class=RiskClass.WRITE_CODE,
            preview=TOOL_PREVIEW,
        )
    service = ApprovalsService(databases, env.manager, store=store, event_log=log)

    async def race() -> None:
        deciding = asyncio.create_task(service.decide(project_id, "apr_decided", approve=True))
        await log.entered.wait()
        stopping = asyncio.create_task(env.manager.stop(project_id, "orchestrator"))
        await store.contended.wait()  # Stop, holding the session lock, waits for the settle lock
        log.release.set()
        await asyncio.gather(deciding, stopping)

    # A deadlock never ends; correct code ends in well under a second, also on a slow runner.
    await asyncio.wait_for(race(), timeout=20)

    decided, stopped = [await store.get(project_id, i) for i in ids]
    assert decided is not None and stopped is not None
    assert (decided.status, decided.settled_at is not None) == ("approved", True)
    assert (stopped.status, stopped.settled_at is not None) == ("cancelled", True)
    message = await env.store.message(project_id, session_id, message_id)
    assert message is not None
    assert message.status == "stopped"
    assert [p.status for p in message.parts if isinstance(p, ApprovalPart)] == [
        "approved",
        "cancelled",
    ]
    assert await env.store.turn_status(project_id, "trn_race") == "stopped"
    await env.manager.close()
