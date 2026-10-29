"""A Stop between the stall and the turn's `waiting` (the sweep at the turn's end settles the
stall; a Try again on the stopped turn is refused)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import TurnEnded
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.errors import DomainError
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import collect_until_turn_ended, opened_stream, until_idle
from tests.agents.runtime.stall_env import KEY, build_stall_env
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


async def test_a_stop_after_the_stall_and_before_waiting_leaves_no_pending_stall(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = await build_stall_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    fake_openai.state.script_for_key(KEY, ["401"])
    set_turn = SessionStore.set_turn
    at_waiting = asyncio.Event()

    async def held_at_waiting(
        self: SessionStore, project_id: str, turn_id: str, status: str, *, error: str | None = None
    ) -> None:
        # The stall is created; the Stop lands before the turn is stored as waiting.
        if status == "waiting":
            at_waiting.set()
            await asyncio.Event().wait()
        await set_turn(self, project_id, turn_id, status, error=error)

    monkeypatch.setattr(SessionStore, "set_turn", held_at_waiting)
    stream = await opened_stream(env.session)
    await env.session.manager.send(env.project_id, "orchestrator", "hi")
    await asyncio.wait_for(at_waiting.wait(), timeout=10)
    await env.session.manager.stop(env.project_id, "orchestrator")
    ended = next(e for e in await collect_until_turn_ended(stream) if isinstance(e, TurnEnded))
    turn_id = ended.data.turn_id

    assert ended.data.status == "stopped"

    # The end is published before the sweep that settles the turn's approvals runs.
    await until_idle(env.session.manager, env.project_id, "orchestrator")
    [stall] = await env.stalls(turn_id)
    assert stall.status == "cancelled"
    pending = await env.session.approval_store.pending(env.project_id)
    assert [a for a in pending if a.turn_id == turn_id] == []
    assert (await env.agent_message(turn_id)).status == "stopped"

    with pytest.raises(DomainError) as refused:
        await env.approvals.decide(env.project_id, stall.id, approve=True, note=None)
    assert refused.value.code == "approval_not_pending"
    # A resume of the stopped turn is refused as well: only a waiting turn is taken.
    session_id = await env.session.store.session_id(env.project_id, "orchestrator")
    assert await env.session.store.turn_status(env.project_id, turn_id) == "stopped"
    requests = len(fake_openai.state.requests)
    await env.session.manager.resume_turn(env.project_id, session_id, turn_id)
    await until_idle(env.session.manager, env.project_id, "orchestrator")
    assert len(fake_openai.state.requests) == requests
    assert await env.session.store.turn_status(env.project_id, turn_id) == "stopped"
    assert await env.event_payloads("turn.resumed") == []
    await env.session.manager.close()
