from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
from pydantic import BaseModel

from mlops_agents.agents.runtime.contract import (
    MessageUpdated,
    Resync,
    TurnEnded,
)
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import AgentTool, RiskClass, ToolContext, ToolOutput
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import build_session_env, collect_until_turn_ended
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.stream_helpers import through_first_delta, through_tool_started


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

    fake_openai.state.hold_slow()
    # Reading the snapshot starts the subscription now, so the held first delta reaches it.
    sub = manager.subscribe(project_id, "orchestrator", after_seq=None)
    assert isinstance(await anext(sub), Resync)

    await manager.send(project_id, "orchestrator", "Slow turn 1")
    await manager.send(project_id, "orchestrator", "Quick turn 2")
    await through_first_delta(sub)

    await manager.stop(project_id, "orchestrator")
    all_events = await collect_until_turn_ended(sub)
    stopped = all_events[-1]
    assert isinstance(stopped, TurnEnded)
    assert stopped.data.status == "stopped"

    # Turn 2 runs on the same held model, so it can only finish once the gate opens.
    fake_openai.state.release_slow()
    turn_2_events = await collect_until_turn_ended(sub, timeout=15.0)
    finished = turn_2_events[-1]
    assert isinstance(finished, TurnEnded)
    assert finished.data.status == "done"

    assert any(
        isinstance(ev, MessageUpdated) and ev.data.message.status == "stopped" for ev in all_events
    )

    store = env.store
    sid = await store.session_id(project_id, "orchestrator")
    msgs = await store.messages(project_id, sid)
    agent_msg_1 = next(m for m in msgs if m.turn_id == stopped.data.turn_id)
    assert agent_msg_1.status == "stopped"
    assert len(agent_msg_1.parts) > 0

    await manager.close()


class HeldArgs(BaseModel):
    pass


class HeldTool(AgentTool[HeldArgs]):
    """Runs until the test releases it, so a Stop lands while the tool is running."""

    name = "HeldTool"
    description = "Waits until the test releases it."
    risk = RiskClass.READ
    args_model = HeldArgs

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def run(self, ctx: ToolContext, args: HeldArgs) -> ToolOutput:
        await self.release.wait()
        return ToolOutput(content="released")


async def test_stop_during_slow_tool(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    # The fake model calls an agent's first tool: an agent with only HeldTool runs it.
    def_dir = tmp_path / "defs"
    def_dir.mkdir()
    (def_dir / "heldagent.md").write_text(
        "---\nname: heldagent\ndisplay_name: Held Agent\ndescription: Test\n"
        "tools:\n  - HeldTool\n---\nPrompt\n"
    )
    held = HeldTool()
    tools = default_registry()
    tools.register(held)
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        extra_dirs=[def_dir],
        tools=tools,
    )
    manager = env.manager
    project_id = env.project_id
    await env.settings.set_default_model("heldagent", f"{env.provider}:fake-tools")

    # Subscribing after the snapshot's last_seq replays anything published before the first read.
    view = await manager.get(project_id, "heldagent")
    sub = manager.subscribe(project_id, "heldagent", after_seq=view.last_seq)
    await manager.send(project_id, "heldagent", "Call the held tool")

    started = await through_tool_started(sub)
    assert started.data.name == "HeldTool"

    await manager.stop(project_id, "heldagent")
    stop_events = await collect_until_turn_ended(sub)
    stopped = stop_events[-1]
    assert isinstance(stopped, TurnEnded)
    assert stopped.data.status == "stopped"
    assert any(
        isinstance(ev, MessageUpdated) and ev.data.message.status == "stopped" for ev in stop_events
    )

    held.release.set()
    m = await manager.send(project_id, "heldagent", "New turn")
    assert m.status == "complete"
    evs = await collect_until_turn_ended(sub)
    assert isinstance(evs[-1], TurnEnded)
    assert evs[-1].data.status == "done"

    await manager.close()
