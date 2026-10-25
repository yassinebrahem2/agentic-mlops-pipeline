"""Stop ends a turn held in the gate's decide or wait step exactly as it ends any other turn."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import TurnEnded
from mlops_agents.agents.runtime.hooks import GateCall, GateDecision
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.hook_fakes import ALLOW, ask
from tests.agents.runtime.session_env import build_session_env, collect_until_turn_ended
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


class HoldingGate:
    """Holds the turn in `decide`, or in `wait` after asking about every call."""

    def __init__(self, *, hold_in: str) -> None:
        self._hold_in = hold_in
        self.held = asyncio.Event()

    async def _hold(self) -> None:
        self.held.set()
        await asyncio.Event().wait()

    async def decide(self, calls: Sequence[GateCall]) -> dict[str, GateDecision]:
        if self._hold_in == "decide":
            await self._hold()
        return {c.tool_call_id: ask(f"apr_{c.tool_call_id}") for c in calls}

    async def wait(
        self, project_id: str, turn_id: str, decisions: dict[str, GateDecision]
    ) -> dict[str, GateDecision]:
        await self._hold()
        return {cid: ALLOW for cid in decisions}


@pytest.mark.parametrize("hold_in", ["decide", "wait"])
async def test_stop_while_the_gate_holds_the_turn_ends_it_stopped(
    hold_in: str,
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    gate = HoldingGate(hold_in=hold_in)
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, gate=gate
    )
    try:
        sub = env.manager.subscribe(env.project_id, "orchestrator", after_seq=None)
        await anext(sub)
        await env.manager.send(env.project_id, "orchestrator", "Read the readme")
        await asyncio.wait_for(gate.held.wait(), timeout=10)

        await env.manager.stop(env.project_id, "orchestrator")
        ended = (await collect_until_turn_ended(sub))[-1]

        assert isinstance(ended, TurnEnded)
        assert ended.data.status == "stopped"
        sid = await env.store.session_id(env.project_id, "orchestrator")
        agent_msg = next(
            m
            for m in await env.store.messages(env.project_id, sid)
            if m.turn_id == ended.data.turn_id and m.author_kind == "agent"
        )
        assert agent_msg.status == "stopped"
        assert [p.type for p in agent_msg.parts] == ["tool_call"]
    finally:
        await env.manager.close()
