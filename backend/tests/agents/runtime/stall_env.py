"""A session stack whose provider has one key, with the approvals service bound and real event
and audit logs, for the stall tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
from sqlalchemy import select

from mlops_agents.agents.approvals.contract import ApprovalView
from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.service import ApprovalsService
from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.hooks.registry import HookRegistry
from mlops_agents.agents.runtime.contract import (
    ApprovalPart,
    MessageView,
    StreamEvent,
    TurnWaiting,
)
from mlops_agents.agents.runtime.graph import turn_checkpointer
from mlops_agents.agents.runtime.hooks import InboundSource
from mlops_agents.agents.tools.base import ToolRegistry
from mlops_agents.events.audit import SqliteAuditLog
from mlops_agents.events.contract import EventType
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.paths import DataPaths
from mlops_agents.providers.failover import Clock
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.sqlite import connect_checkpointer
from mlops_agents.storage.tables import audit
from tests.agents.runtime.session_env import SessionEnv, build_session_env, opened_stream
from tests.agents.runtime.test_resuming_turns import collect_until_turn_waiting
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

KEY = "gsk_only_stall_secret"


@dataclass(frozen=True)
class StallEnv:
    session: SessionEnv
    approvals: ApprovalsService
    events: SqliteEventLog
    audit: SqliteAuditLog
    databases: Databases

    @property
    def project_id(self) -> str:
        return self.session.project_id

    async def stalls(self, turn_id: str) -> list[ApprovalView]:
        """The turn's stall approvals, oldest first."""
        records = await self.session.approval_store.list_for_step(self.project_id, turn_id)
        return [r for r in records if r.kind == "stall"]

    async def event_payloads(self, event_type: EventType) -> list[dict[str, Any]]:
        """The payloads of the recorded events of one type, oldest first."""
        records = await self.events.history(self.project_id, types=[event_type])
        return [r.payload for r in reversed(records)]

    async def audit_rows(self) -> list[dict[str, Any]]:
        engine = await self.databases.project(self.project_id)
        async with engine.connect() as conn:
            return [dict(r) for r in (await conn.execute(select(audit))).mappings().all()]

    async def agent_message(self, turn_id: str, agent: str = "orchestrator") -> MessageView:
        session_id = await self.session.store.session_id(self.project_id, agent)
        message = await self.session.store.turn_agent_message(self.project_id, session_id, turn_id)
        assert message is not None
        return message

    async def checkpoint(self, turn_id: str) -> dict[str, Any] | None:
        """The turn's checkpointed state, read back as the turn's resume reads it."""
        conn = await connect_checkpointer(Path(self.session.store.db_path(self.project_id)))
        try:
            saver = turn_checkpointer(conn)
            await saver.setup()
            found = await saver.aget_tuple({"configurable": {"thread_id": turn_id}})
        finally:
            await conn.close()
        return None if found is None else dict(found.checkpoint["channel_values"])


def stall_parts(message: MessageView) -> list[ApprovalPart]:
    return [p for p in message.parts if isinstance(p, ApprovalPart) and p.kind == "stall"]


async def build_stall_env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    *,
    clock: Clock | None = None,
    tools: ToolRegistry | None = None,
    extra_dirs: list[Path] | None = None,
    inbound: InboundSource | None = None,
    hooks: HookRegistry | None = None,
    keys: Sequence[str] = (KEY,),
) -> StallEnv:
    """`keys` are the provider's keys, in order; none makes it keyless."""
    events = SqliteEventLog(databases)
    audit_log = SqliteAuditLog(databases, secrets=[])
    store = ApprovalStore(databases)
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake,
        model_http,
        git_env,
        tools=tools,
        extra_dirs=extra_dirs,
        inbound=inbound,
        gate=ApprovalGate(databases, store=store, audit_log=audit_log, event_log=events),
        audit_log=audit_log,
        approval_store=store,
        event_log=events,
        clock=clock,
        hooks=hooks,
    )
    approvals = ApprovalsService(
        databases, env.manager, store=store, audit_log=audit_log, event_log=events
    )
    env.manager.bind_approvals(approvals)
    for key in keys:
        await env.providers.add_key(env.provider, key)
    return StallEnv(env, approvals, events, audit_log, databases)


async def send_until_waiting(
    env: StallEnv, text: str, agent: str = "orchestrator"
) -> tuple[AsyncGenerator[StreamEvent, None], list[StreamEvent], str]:
    """Send `text` and wait for the turn to wait; the open stream, its events and the turn."""
    stream = await opened_stream(env.session, agent)
    await env.session.manager.send(env.project_id, agent, text)
    events = await collect_until_turn_waiting(stream)
    waiting = next(e for e in events if isinstance(e, TurnWaiting))
    return stream, events, waiting.data.turn_id
