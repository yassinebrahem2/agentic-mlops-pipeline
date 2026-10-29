from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest
from pydantic import BaseModel

from mlops_agents.agents.runtime.contract import (
    MessageUpdated,
    Resync,
    TurnEnded,
)
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import AgentTool, RiskClass, ToolContext, ToolOutput
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.observer_fakes import HeldObserver, turn_statuses
from tests.agents.runtime.session_env import (
    build_session_env,
    collect_until_turn_ended,
    until_idle,
)
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
    await until_idle(manager, project_id, "heldagent")
    m = await manager.send(project_id, "heldagent", "New turn")
    assert m.status == "complete"
    evs = await collect_until_turn_ended(sub)
    assert isinstance(evs[-1], TurnEnded)
    assert evs[-1].data.status == "done"

    await manager.close()


async def test_stop_while_a_turn_started_observer_runs_stops_the_turn(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    held = HeldObserver()
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        observers=[held],
    )
    manager = env.manager
    view = await manager.get(env.project_id, "orchestrator")
    sub = manager.subscribe(env.project_id, "orchestrator", after_seq=view.last_seq)

    await manager.send(env.project_id, "orchestrator", "Check readme")
    await asyncio.wait_for(held.entered.wait(), timeout=5)
    requests_before = len(fake_openai.state.requests)
    # The turn has started but its observers have not returned yet.
    await manager.stop(env.project_id, "orchestrator")
    held.release.set()

    stopped = (await collect_until_turn_ended(sub))[-1]
    assert isinstance(stopped, TurnEnded)
    assert stopped.data.status == "stopped"
    assert await asyncio.wait_for(held.ended.get(), timeout=5) == "stopped"
    assert len(fake_openai.state.requests) == requests_before

    # The session goes on: the next turn runs to its end.
    await manager.send(env.project_id, "orchestrator", "Check readme again")
    after = (await collect_until_turn_ended(sub))[-1]
    assert isinstance(after, TurnEnded)
    assert after.data.status == "done"
    # The observer heard each turn end exactly once.
    assert await asyncio.wait_for(held.ended.get(), timeout=5) == "done"
    assert held.ended.qsize() == 0
    assert [call[0] for call in held.calls] == ["started", "ended", "started", "ended"]
    assert sorted(await turn_statuses(databases, env.project_id)) == ["done", "stopped"]

    await manager.close()


def _hold_next_queued_turn(
    monkeypatch: pytest.MonkeyPatch, call: int
) -> tuple[asyncio.Event, asyncio.Event, asyncio.Event]:
    """Hold the worker's `call`-th look for a queued turn (1 is its first), under the lock.
    The third event is set when that look returns; the worker acts on it before it yields."""
    orig = SessionStore.next_queued_turn
    entered = asyncio.Event()
    release = asyncio.Event()
    returned = asyncio.Event()
    calls = 0

    async def held(self: SessionStore, project_id: str, session_id: str) -> tuple[str, str] | None:
        nonlocal calls
        calls += 1
        if calls != call:
            return await orig(self, project_id, session_id)
        entered.set()
        await release.wait()
        result = await orig(self, project_id, session_id)
        returned.set()
        return result

    monkeypatch.setattr(SessionStore, "next_queued_turn", held)
    return entered, release, returned


async def test_stop_before_the_worker_takes_the_queued_turn_stops_it(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    manager = env.manager
    view = await manager.get(env.project_id, "orchestrator")
    sub = manager.subscribe(env.project_id, "orchestrator", after_seq=view.last_seq)
    entered, release, _ = _hold_next_queued_turn(monkeypatch, call=1)

    await manager.send(env.project_id, "orchestrator", "Check readme")
    await asyncio.wait_for(entered.wait(), timeout=5)
    # Right after the Send: the session is busy and its turn queued, but the worker has not
    # taken it yet (its first look is held).
    await manager.stop(env.project_id, "orchestrator")
    release.set()

    first = (await collect_until_turn_ended(sub))[-1]
    assert isinstance(first, TurnEnded)
    assert first.data.status == "stopped"

    # The Stop was for that turn only: the next one runs to its end.
    await manager.send(env.project_id, "orchestrator", "Check readme again")
    second = (await collect_until_turn_ended(sub))[-1]
    assert isinstance(second, TurnEnded)
    assert second.data.status == "done"
    assert sorted(await turn_statuses(databases, env.project_id)) == ["done", "stopped"]

    await manager.close()


async def test_stop_while_idle_does_not_stop_the_next_turn(
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
    view = await manager.get(env.project_id, "orchestrator")
    sub = manager.subscribe(env.project_id, "orchestrator", after_seq=view.last_seq)

    await manager.stop(env.project_id, "orchestrator")
    await manager.send(env.project_id, "orchestrator", "Check readme")

    ended = (await collect_until_turn_ended(sub))[-1]
    assert isinstance(ended, TurnEnded)
    assert ended.data.status == "done"

    await manager.close()


async def test_stop_as_the_worker_finds_no_more_turns_does_not_stop_the_next_turn(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    manager = env.manager
    view = await manager.get(env.project_id, "orchestrator")
    sub = manager.subscribe(env.project_id, "orchestrator", after_seq=view.last_seq)
    # The second look comes after the first turn ended, and finds the queue empty.
    entered, release, returned = _hold_next_queued_turn(monkeypatch, call=2)

    await manager.send(env.project_id, "orchestrator", "Check readme")
    first = (await collect_until_turn_ended(sub))[-1]
    await asyncio.wait_for(entered.wait(), timeout=5)
    await manager.stop(env.project_id, "orchestrator")
    release.set()
    await asyncio.wait_for(returned.wait(), timeout=5)
    assert (await manager.get(env.project_id, "orchestrator")).busy is False

    await manager.send(env.project_id, "orchestrator", "Check readme again")
    second = (await collect_until_turn_ended(sub))[-1]
    assert isinstance(first, TurnEnded) and first.data.status == "done"
    assert isinstance(second, TurnEnded) and second.data.status == "done"

    await manager.close()


async def test_stop_between_two_queued_turns_stops_neither(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    manager = env.manager
    view = await manager.get(env.project_id, "orchestrator")
    sub = manager.subscribe(env.project_id, "orchestrator", after_seq=view.last_seq)
    # The second look comes after the first turn ended, and finds the second turn queued.
    entered, release, _ = _hold_next_queued_turn(monkeypatch, call=2)

    await manager.send(env.project_id, "orchestrator", "Check readme")
    queued = await manager.send(env.project_id, "orchestrator", "Check readme again")
    first = (await collect_until_turn_ended(sub))[-1]
    await asyncio.wait_for(entered.wait(), timeout=5)
    # As in stage 1, a Stop between two turns has no turn to stop and is dropped.
    await manager.stop(env.project_id, "orchestrator")
    release.set()
    second = (await collect_until_turn_ended(sub))[-1]

    assert queued.status == "queued"
    assert isinstance(first, TurnEnded) and first.data.status == "done"
    assert isinstance(second, TurnEnded) and second.data.status == "done"
    assert await turn_statuses(databases, env.project_id) == ["done", "done"]

    await manager.close()
