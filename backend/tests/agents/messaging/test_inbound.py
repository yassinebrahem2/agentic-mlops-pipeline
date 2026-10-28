"""Tests for mid-turn delivery (Task 2 of Stage 2 Messaging)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Any

import httpx2
import pytest
from langgraph.types import interrupt
from sqlalchemy import select

from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.messaging.bus import SqliteMessageBus
from mlops_agents.agents.messaging.inbound import SqliteInboundSource
from mlops_agents.agents.messaging.settings import MessagingSettings
from mlops_agents.agents.runtime.contract import StreamEvent, TurnEnded, TurnWaiting
from mlops_agents.agents.runtime.hooks import Gate, GateCall, GateDecision
from mlops_agents.agents.runtime.model_caller import TurnFailed
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.tools import default_registry
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import turns
from tests.agents.messaging.bus_env import BusEnv
from tests.agents.runtime.hook_fakes import ALLOW
from tests.agents.runtime.session_env import (
    build_session_env,
    collect_until_turn_ended,
)
from tests.conftest import STAGE_2_TOOLS, MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

NO_TURN = "trn_none"

INBOUND_DEFINITIONS = {
    "alpha": "can_message: [beta]\ntools: [SendMessage]",
    "beta": "can_message: [alpha]\ntools: [Read]",
    "gamma": "can_message: []",
}


async def collect_until_turn_waiting(
    sub: AsyncGenerator[StreamEvent, None],
    timeout: float = 10.0,  # noqa: ASYNC109
) -> list[StreamEvent]:
    events: list[StreamEvent] = []

    async def collector() -> None:
        while True:
            ev = await anext(sub)
            events.append(ev)
            if isinstance(ev, TurnWaiting):
                break

    await asyncio.wait_for(collector(), timeout=timeout)
    return events


class HoldingGate(Gate):
    """Holds execution in `decide` until released."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def decide(self, calls: Sequence[GateCall]) -> dict[str, GateDecision]:
        self.entered.set()
        await self.release.wait()
        return {c.tool_call_id: ALLOW for c in calls}

    async def wait(
        self, project_id: str, turn_id: str, decisions: dict[str, GateDecision]
    ) -> dict[str, GateDecision]:
        return decisions


class ResumingAskGate(Gate):
    """Asks for approval on the first tool call, triggering interrupt and waiting state."""

    def __init__(self) -> None:
        self.asked = False

    async def decide(self, calls: Sequence[GateCall]) -> dict[str, GateDecision]:
        if not self.asked:
            self.asked = True
            results: dict[str, GateDecision] = {}
            for call in calls:
                apr_id = f"apr_{call.tool_call_id}"
                results[call.tool_call_id] = GateDecision(
                    outcome="ask",
                    message=None,
                    approval_id=apr_id,
                )
            return results
        return {c.tool_call_id: ALLOW for c in calls}

    async def wait(
        self, project_id: str, turn_id: str, decisions: dict[str, GateDecision]
    ) -> dict[str, GateDecision]:
        if any(d.outcome == "ask" for d in decisions.values()):
            interrupt(None)
        return {cid: ALLOW for cid in decisions}


async def build_inbound_env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    *,
    gate: Gate | None = None,
) -> tuple[BusEnv, SqliteInboundSource]:
    defs = tmp_path / "defs"
    defs.mkdir()
    for name, line in INBOUND_DEFINITIONS.items():
        (defs / f"{name}.md").write_text(
            f"---\nname: {name}\ndisplay_name: {name.title()}\ndescription: {name}\n{line}\n"
            f"---\nYou are {name}.",
            encoding="utf-8",
        )
    events = SqliteEventLog(databases)
    settings = MessagingSettings(databases)
    bus = SqliteMessageBus(databases, events, settings)
    tools = default_registry(bus=bus)
    bus.use_catalog(AgentCatalog.load(tools.names() | STAGE_2_TOOLS, extra_dirs=[defs]))

    inbound = SqliteInboundSource(databases)

    session = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake,
        model_http,
        git_env,
        extra_dirs=[defs],
        tools=tools,
        bus=bus,
        gate=gate,
        inbound=inbound,
        observers=[inbound],
    )
    inbound.bind(session.manager)

    for name in INBOUND_DEFINITIONS:
        await session.settings.set_default_model(name, f"{session.provider}:fake-tools")

    bus_env = BusEnv(session=session, bus=bus, settings=settings, events=events, fake=fake)
    return bus_env, inbound


async def test_a_message_posted_while_recipient_runs_a_slow_tool_appears_in_its_next_model_request(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    gate = HoldingGate()
    env, _ = await build_inbound_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, gate=gate
    )
    project_id = env.session.project_id
    manager = env.session.manager

    # Subscribe to beta's events
    view = await manager.get(project_id, "beta")
    sub = manager.subscribe(project_id, "beta", after_seq=view.last_seq)

    # Start beta's turn with an initial message
    await manager.send(project_id, "beta", "Inspect the repository.")

    # Beta runs step 1 (model calls a tool), enters gate.decide and holds
    await gate.entered.wait()

    # While recipient runs the tool, alpha posts a message to beta
    await env.bus.send(
        project_id, sender="alpha", sender_turn_id="trn_test", to="beta", text="Check branch too."
    )

    # Release the gate to let the tool complete
    gate.release.set()

    # Wait for turn to end
    events = await collect_until_turn_ended(sub)
    turn_ended = next(e for e in events if isinstance(e, TurnEnded))
    assert turn_ended.data.status == "done"

    # Find model requests for beta
    beta_requests = [
        r for r in fake_openai.state.requests if "You are beta" in json.dumps(r["messages"])
    ]
    assert len(beta_requests) >= 2  # Step 1: initial tool call; Step 2: after tool results
    second_request = beta_requests[1]
    msgs = second_request["messages"]

    # Verify that in step 2:
    # 1. Tool result is present
    # 2. Inbound message is present AFTER the tool result
    tool_idx = next(i for i, m in enumerate(msgs) if m.get("role") == "tool")
    inbound_idx = next(
        i
        for i, m in enumerate(msgs)
        if m.get("role") == "user" and "Message from alpha:" in str(m.get("content"))
    )
    assert inbound_idx > tool_idx
    assert "Check branch too." in str(msgs[inbound_idx].get("content"))

    # Verify NO extra turn was started
    workers_mgr: Any = getattr(manager, "_workers", None)
    if workers_mgr is not None:
        worker_task = workers_mgr.get((project_id, view.id))
        if worker_task is not None:
            await worker_task

    beta_view = await manager.get(project_id, "beta")
    assert not beta_view.busy
    store: Any = getattr(manager, "_store", None)
    assert store is not None
    queued_turn = await store.next_queued_turn(project_id, beta_view.id)
    assert queued_turn is None
    await manager.close()


async def test_a_message_posted_after_the_last_model_step_starts_the_next_turn(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env, _ = await build_inbound_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    project_id = env.session.project_id
    manager = env.session.manager

    # Alpha sends initial message to beta; wait for turn 1 to complete
    view = await manager.get(project_id, "beta")
    sub1 = manager.subscribe(project_id, "beta", after_seq=view.last_seq)
    await env.bus.send(project_id, sender="alpha", sender_turn_id=NO_TURN, to="beta", text="First.")
    events1 = await collect_until_turn_ended(sub1)
    turn_1_id = next(e for e in events1 if isinstance(e, TurnEnded)).data.turn_id

    # Beta is idle after turn 1. A message posted now starts turn 2.
    view2 = await manager.get(project_id, "beta")
    sub2 = manager.subscribe(project_id, "beta", after_seq=view2.last_seq)
    await env.bus.send(
        project_id, sender="alpha", sender_turn_id="trn_other", to="beta", text="Second."
    )
    events2 = await collect_until_turn_ended(sub2)
    turn_2_id = next(e for e in events2 if isinstance(e, TurnEnded)).data.turn_id

    assert turn_2_id != turn_1_id

    # Verify turn 2 answered the second message
    latest_req = fake_openai.state.requests[-1]
    assert "Message from alpha:" in json.dumps(latest_req["messages"])
    assert "Second." in json.dumps(latest_req["messages"])
    await manager.close()


async def test_a_queued_user_message_is_not_taken_mid_turn(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    gate = HoldingGate()
    env, _ = await build_inbound_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, gate=gate
    )
    project_id = env.session.project_id
    manager = env.session.manager

    view = await manager.get(project_id, "beta")
    sub = manager.subscribe(project_id, "beta", after_seq=view.last_seq)

    # Start turn 1 with initial message
    await manager.send(project_id, "beta", "Initial turn message.")
    await gate.entered.wait()

    # Post a USER message while beta is held mid-turn
    await manager.send(project_id, "beta", "Queued user input.")

    # Release the gate to let turn 1 continue
    gate.release.set()

    # Collect turn 1 end
    events1 = await collect_until_turn_ended(sub)
    turn_1_id = next(e for e in events1 if isinstance(e, TurnEnded)).data.turn_id

    # Check step 2 of turn 1: user message was NOT included mid-turn
    beta_requests = [
        r for r in fake_openai.state.requests if "You are beta" in json.dumps(r["messages"])
    ]
    assert len(beta_requests) >= 2
    step_2_msgs = json.dumps(beta_requests[1]["messages"])
    assert "Queued user input." not in step_2_msgs

    # User message starts turn 2
    events2 = await collect_until_turn_ended(sub)
    turn_2_id = next(e for e in events2 if isinstance(e, TurnEnded)).data.turn_id
    assert turn_2_id != turn_1_id

    # Turn 2 has the user message
    turn_2_msgs = json.dumps(fake_openai.state.requests[-1]["messages"])
    assert "Queued user input." in turn_2_msgs
    await manager.close()


async def test_a_waiting_turn_gets_the_message_at_its_first_step_after_resuming(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    gate = ResumingAskGate()
    env, _ = await build_inbound_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, gate=gate
    )
    project_id = env.session.project_id
    manager = env.session.manager

    view = await manager.get(project_id, "beta")
    sub = manager.subscribe(project_id, "beta", after_seq=view.last_seq)

    # Start beta turn; tool call will be interrupted into "waiting"
    await manager.send(project_id, "beta", "Start and wait.")
    waiting_events = await collect_until_turn_waiting(sub)
    turn_id = next(e for e in waiting_events if isinstance(e, TurnWaiting)).data.turn_id

    # While waiting, alpha sends a message to beta
    await env.bus.send(
        project_id,
        sender="alpha",
        sender_turn_id="trn_other",
        to="beta",
        text="Arrived while waiting.",
    )

    # Resume the turn
    view_beta = await manager.get(project_id, "beta")
    await manager.resume_turn(project_id, view_beta.id, turn_id)

    # Turn resumes, tool runs, model step takes inbound message and finishes
    events = await collect_until_turn_ended(sub)
    turn_ended = next(e for e in events if isinstance(e, TurnEnded))
    assert turn_ended.data.status == "done"

    # Verify that in the first model request after resuming, the inbound message is present
    last_req = fake_openai.state.requests[-1]
    assert "Message from alpha:" in json.dumps(last_req["messages"])
    assert "Arrived while waiting." in json.dumps(last_req["messages"])
    await manager.close()


async def test_a_message_taken_by_a_turn_that_does_not_end_done_is_delivered_again(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = HoldingGate()
    env, _ = await build_inbound_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, gate=gate
    )
    project_id = env.session.project_id
    manager = env.session.manager

    view = await manager.get(project_id, "beta")
    sub = manager.subscribe(project_id, "beta", after_seq=view.last_seq)

    # Start beta turn 1
    await manager.send(project_id, "beta", "First turn.")
    await gate.entered.wait()

    # Alpha sends message mid-turn
    await env.bus.send(
        project_id, sender="alpha", sender_turn_id="trn_test", to="beta", text="Crucial update."
    )

    # Make the model call fail immediately after taking the mid-turn message
    called = False
    caller: Any = getattr(env.session.manager, "_caller", None)
    assert caller is not None
    original_call = caller.call

    async def failing_call(*args: Any, **kwargs: Any) -> Any:
        nonlocal called
        if not called and gate.release.is_set():
            called = True
            raise TurnFailed("Simulated failure after take")
        return await original_call(*args, **kwargs)

    monkeypatch.setattr(caller, "call", failing_call)

    # Release gate so beta moves to model step, which takes the message and then fails
    gate.release.set()

    # Collect events: turn 1 ends failed
    events1 = await collect_until_turn_ended(sub)
    turn_1_ended = next(e for e in events1 if isinstance(e, TurnEnded))
    assert turn_1_ended.data.status == "failed"

    # Turn 2 starts automatically to deliver the message again!
    events2 = await collect_until_turn_ended(sub)
    turn_2_ended = next(e for e in events2 if isinstance(e, TurnEnded))
    assert turn_2_ended.data.status == "done"
    assert turn_2_ended.data.turn_id != turn_1_ended.data.turn_id

    # In turn 2, beta answers the redelivered message
    turn_2_msgs = json.dumps(fake_openai.state.requests[-1]["messages"])
    assert "Message from alpha:" in turn_2_msgs
    assert "Crucial update." in turn_2_msgs
    await manager.close()


async def test_a_message_taken_by_a_running_turn_is_redelivered_on_crash_recovery(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    gate = HoldingGate()
    env, _inbound = await build_inbound_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, gate=gate
    )
    project_id = env.session.project_id
    manager = env.session.manager
    store = SessionStore(databases)

    view = await manager.get(project_id, "beta")

    # Start beta turn 1
    await manager.send(project_id, "beta", "First turn.")
    await gate.entered.wait()

    # Alpha sends message mid-turn
    await env.bus.send(
        project_id,
        sender="alpha",
        sender_turn_id="trn_test",
        to="beta",
        text="Crash-recovery test.",
    )

    # Release gate so beta takes the inbound message mid-turn
    gate.release.set()

    # Wait until turn 1 is running and has taken the message
    while True:
        msgs = await store.messages(project_id, view.id)
        mid_msg = next((m for m in msgs if m.author_name == "Alpha"), None)
        if mid_msg is not None and mid_msg.status == "complete":
            break
        await asyncio.sleep(0.01)

    # Simulate abrupt process crash: stop manager without letting normal turn end execute
    await manager.close()

    # On restart, recover() is called
    await store.recover(project_id, approval_store=ApprovalStore(databases))

    # Verify that the mid-turn message is redelivered (queued again)
    msgs = await store.messages(project_id, view.id)
    redelivered = next(m for m in msgs if m.author_name == "Alpha")
    assert redelivered.status == "queued"

    # Verify a new queued turn was created for it
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        turn_rows = (
            (
                await conn.execute(
                    select(turns).where(
                        turns.c.session_id == view.id,
                        turns.c.status == "queued",
                        turns.c.trigger_message_id == redelivered.id,
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(turn_rows) == 1
