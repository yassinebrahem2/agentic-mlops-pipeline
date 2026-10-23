from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
from pydantic import BaseModel

from mlops_agents.agents.runtime.contract import (
    MessageUpdated,
    StreamEvent,
    TextDelta,
    ToolStarted,
    TurnEnded,
)
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import AgentTool, RiskClass, ToolContext, ToolOutput
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import build_session_env, collect_until_turn_ended
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


async def test_stop_during_fake_slow(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    manager = env.manager
    project_id = env.project_id
    prov_name = env.provider
    settings_svc = env.settings
    await settings_svc.set_default_model("orchestrator", f"{prov_name}:fake-slow")

    sub = manager.subscribe(project_id, "orchestrator", after_seq=None)

    await manager.send(project_id, "orchestrator", "Slow turn 1")
    await manager.send(project_id, "orchestrator", "Quick turn 2")

    async for ev in sub:
        if isinstance(ev, TextDelta):
            break

    await manager.stop(project_id, "orchestrator")

    all_events: list[StreamEvent] = []
    ended: list[TurnEnded] = []
    async for ev in sub:
        all_events.append(ev)
        if isinstance(ev, TurnEnded):
            ended.append(ev)
            if len(ended) == 2:
                break

    assert ended[0].data.status == "stopped"
    assert ended[1].data.status == "done"
    assert any(
        isinstance(ev, MessageUpdated) and ev.data.message.status == "stopped" for ev in all_events
    )

    store = env.store
    sid = await store.session_id(project_id, "orchestrator")
    msgs = await store.messages(project_id, sid)
    agent_msg_1 = next(m for m in msgs if m.turn_id == ended[0].data.turn_id)
    assert agent_msg_1.status == "stopped"
    assert len(agent_msg_1.parts) > 0

    await manager.close()


class SlowDummyArgs(BaseModel):
    delay: float = 2.0


class SlowTool(AgentTool[SlowDummyArgs]):
    name = "slow_sleep"
    description = "Sleeps for a while."
    risk = RiskClass.READ
    args_model = SlowDummyArgs

    async def run(self, ctx: ToolContext, args: SlowDummyArgs) -> ToolOutput:
        await asyncio.sleep(args.delay)
        return ToolOutput(content="done")


async def test_stop_during_slow_tool(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    tools = default_registry()
    tools.register(SlowTool())
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, tools=tools
    )
    manager = env.manager
    project_id = env.project_id

    sub = manager.subscribe(project_id, "orchestrator", after_seq=None)
    await manager.send(project_id, "orchestrator", "Call slow sleep")

    async for ev in sub:
        if isinstance(ev, ToolStarted):
            break

    await manager.stop(project_id, "orchestrator")

    stop_events: list[StreamEvent] = []
    async for ev in sub:
        stop_events.append(ev)
        if isinstance(ev, TurnEnded):
            assert ev.data.status == "stopped"
            break

    assert any(
        isinstance(ev, MessageUpdated) and ev.data.message.status == "stopped" for ev in stop_events
    )

    m = await manager.send(project_id, "orchestrator", "New turn")
    assert m.status == "complete"
    evs = await collect_until_turn_ended(sub)
    assert isinstance(evs[-1], TurnEnded)
    assert evs[-1].data.status == "done"

    await manager.close()
