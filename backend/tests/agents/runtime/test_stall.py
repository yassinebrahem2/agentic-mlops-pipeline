"""The stall (stage 3 spec, section 2): a turn whose provider has no usable key pauses on a
`stall` approval, resumes from its checkpoint on Try again and ends `stopped` on Stop. A real
graph with a checkpointer, the fake server and a fake clock."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from mlops_agents.agents.runtime.contract import (
    ApprovalPart,
    MessageUpdated,
    MessageView,
    StreamEvent,
    TextPart,
    TurnEnded,
)
from mlops_agents.agents.runtime.stall import StallState
from mlops_agents.agents.tools.base import Preview
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.caller_env import fail_after_text
from tests.agents.runtime.hook_fakes import ScriptedInbound
from tests.agents.runtime.session_env import collect_until_turn_ended, opened_stream
from tests.agents.runtime.stall_env import (
    KEY,
    StallEnv,
    build_stall_env,
    send_until_waiting,
    stall_parts,
)
from tests.agents.runtime.test_resuming_turns import (
    DummyWriteTool,
    collect_until_turn_waiting,
    make_tool_reg,
    write_agent_def,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.providers.fake_clock import FakeClock, rate_limit

REJECTED = "Key 1: rejected by the provider."


@pytest.fixture
async def env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> StallEnv:
    return await build_stall_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )


def _last_update(events: list[StreamEvent], message_id: str) -> MessageView:
    return [
        e.data.message
        for e in events
        if isinstance(e, MessageUpdated) and e.data.message.id == message_id
    ][-1]


def _ended(events: list[StreamEvent]) -> TurnEnded:
    return next(e for e in events if isinstance(e, TurnEnded))


async def _provider_id(env: StallEnv) -> str:
    return (await env.session.providers.get_provider(env.session.provider)).id


async def test_every_key_exhausted_pauses_the_turn_on_one_stall(
    env: StallEnv, fake_openai: RunningFake
) -> None:
    fake_openai.state.script_for_key(KEY, ["401"])

    _, events, turn_id = await send_until_waiting(env, "hi")

    [stall] = await env.stalls(turn_id)
    preview = Preview(kind="text", title="Stalled: no usable key", body=REJECTED)
    assert (stall.status, stall.step, stall.tool_call_id, stall.preview) == (
        "pending",
        0,
        None,
        preview,
    )
    assert await env.event_payloads("turn.stalled") == [
        {"turn_id": turn_id, "provider": await _provider_id(env), "model": "fake-tools"}
    ]
    assert await env.audit_rows() == []
    card = ApprovalPart(
        type="approval",
        approval_id=stall.id,
        tool_call_id=None,
        preview=preview,
        status="pending",
        note=None,
        kind="stall",
        step=0,
    )
    message = await env.agent_message(turn_id)
    assert message.status == "waiting"
    assert message.parts == [card]
    assert _last_update(events, message.id).parts == [card]
    # No model message and no step spent: the checkpoint resumes at the same step.
    values = await env.checkpoint(turn_id)
    assert values is not None
    assert values["step_count"] == 0
    assert not any(isinstance(m, AIMessage) for m in values["messages"])
    assert values["stall"] == StallState(
        approval_id=stall.id,
        provider_id=await _provider_id(env),
        model="fake-tools",
        step=0,
        preview=preview,
    )
    await env.session.manager.close()


async def test_try_again_resumes_the_turn_from_its_checkpoint(
    env: StallEnv, fake_openai: RunningFake
) -> None:
    fake_openai.state.script_for_key(KEY, ["401"])
    stream, _, turn_id = await send_until_waiting(env, "hi")
    [stall] = await env.stalls(turn_id)
    stalled_request = len(fake_openai.state.requests) - 1

    await env.approvals.decide(env.project_id, stall.id, approve=True, note=None)
    ended = _ended(await collect_until_turn_ended(stream))

    assert ended.data.status == "done"
    assert await env.event_payloads("turn.resumed") == [{"turn_id": turn_id}]
    # Try again cleared the rejected key: the model was asked the same request again.
    requests = fake_openai.state.requests
    assert requests[stalled_request + 1]["messages"] == requests[stalled_request]["messages"]
    provider = await env.session.providers.get_provider(env.session.provider)
    assert provider.status == "ok"
    message = await env.agent_message(turn_id)
    assert message.status == "complete"
    assert [(p.approval_id, p.status) for p in stall_parts(message)] == [(stall.id, "approved")]
    assert message.parts[0] == stall_parts(message)[0]
    assert isinstance(message.parts[-1], TextPart)
    view = await env.session.manager.get(env.project_id, "orchestrator")
    assert [(m.author_kind, m.id == message.id) for m in view.messages] == [
        ("user", False),
        ("agent", True),
    ]
    assert [r for r in await env.audit_rows() if r["action"] == "approval"] == []
    assert await env.checkpoint(turn_id) is None
    await env.session.manager.close()


async def test_a_stall_keeps_the_inbound_messages_its_step_took(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    # Taken messages are marked read by the turn: dropped here, a turn ending done loses them.
    inbound = HumanMessage(content="Message from writer:\nhello")
    env = await build_stall_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        inbound=ScriptedInbound([[inbound]]),
    )
    fake_openai.state.script_for_key(KEY, ["401"])
    stream, _, turn_id = await send_until_waiting(env, "hi")
    values = await env.checkpoint(turn_id)
    assert values is not None
    assert values["messages"][-1].content == inbound.content
    [stall] = await env.stalls(turn_id)
    resumed_request = len(fake_openai.state.requests)

    await env.approvals.decide(env.project_id, stall.id, approve=True, note=None)
    ended = _ended(await collect_until_turn_ended(stream))

    assert ended.data.status == "done"
    # The second take hands out nothing: only the kept state can still send the message.
    assert fake_openai.state.requests[resumed_request]["messages"][-1] == {
        "content": inbound.content,
        "role": "user",
    }
    await env.session.manager.close()


@pytest.mark.parametrize(("stop_by", "status"), [("card", "rejected"), ("stop", "cancelled")])
async def test_stop_ends_a_stalled_turn_stopped(
    env: StallEnv, fake_openai: RunningFake, stop_by: str, status: str
) -> None:
    fake_openai.state.script_for_key(KEY, ["401"])
    stream, _, turn_id = await send_until_waiting(env, "hi")
    [stall] = await env.stalls(turn_id)

    if stop_by == "card":
        await env.approvals.decide(env.project_id, stall.id, approve=False, note=None)
    else:
        await env.session.manager.stop(env.project_id, "orchestrator")
    ended = _ended(await collect_until_turn_ended(stream))

    assert ended.data.status == "stopped"
    assert [s.status for s in await env.stalls(turn_id)] == [status]
    assert (await env.agent_message(turn_id)).status == "stopped"
    assert await env.checkpoint(turn_id) is None
    assert await env.event_payloads("turn.resumed") == []
    await env.session.manager.close()


async def test_stop_during_a_rate_limit_wait_ends_the_turn_stopped(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    clock = FakeClock()
    clock.hold = asyncio.Event()
    env = await build_stall_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, clock=clock
    )
    fake_openai.state.script_for_key(KEY, [("429", 5.0)])
    stream = await opened_stream(env.session)

    await env.session.manager.send(env.project_id, "orchestrator", "hi")
    await asyncio.wait_for(clock.sleeping.wait(), timeout=10)
    await env.session.manager.stop(env.project_id, "orchestrator")
    ended = _ended(await collect_until_turn_ended(stream))

    assert ended.data.status == "stopped"
    assert clock.sleeps == [5.0]
    assert await env.stalls(ended.data.turn_id) == []
    assert (await env.agent_message(ended.data.turn_id)).status == "stopped"
    assert await env.checkpoint(ended.data.turn_id) is None
    await env.session.manager.close()


async def test_exhausted_again_after_try_again_asks_a_second_stall(
    env: StallEnv, fake_openai: RunningFake
) -> None:
    fake_openai.state.script_for_key(KEY, ["401", "401"])
    stream, _, turn_id = await send_until_waiting(env, "hi")
    [first] = await env.stalls(turn_id)

    await env.approvals.decide(env.project_id, first.id, approve=True, note=None)
    events = await collect_until_turn_waiting(stream)

    decided, second = await env.stalls(turn_id)
    assert (decided.id, decided.status) == (first.id, "approved")
    assert second.id != first.id
    assert (second.status, second.step) == ("pending", 0)
    cards = [(first.id, "approved"), (second.id, "pending")]
    message = await env.agent_message(turn_id)
    assert [(p.approval_id, p.status) for p in stall_parts(message)] == cards
    assert [(p.approval_id, p.status) for p in stall_parts(_last_update(events, message.id))] == (
        cards
    )
    assert len(await env.event_payloads("turn.stalled")) == 2

    await env.approvals.decide(env.project_id, second.id, approve=True, note=None)
    assert _ended(await collect_until_turn_ended(stream)).data.status == "done"
    assert len(stall_parts(await env.agent_message(turn_id))) == 2
    await env.session.manager.close()


async def test_compaction_out_of_keys_fails_the_turn_without_a_stall(
    env: StallEnv, fake_openai: RunningFake
) -> None:
    # Three earlier turns past 75% of the model's context window: the turn compacts first.
    session_id = await env.session.store.session_id(env.project_id, "orchestrator")
    for _ in range(3):
        await env.session.store.add_message(
            env.project_id,
            session_id,
            author_kind="user",
            author_name="user",
            parts=[TextPart(type="text", text="x" * 40_000)],
            status="complete",
        )
    fake_openai.state.script_for_key(KEY, ["401"])
    stream = await opened_stream(env.session)

    await env.session.manager.send(env.project_id, "orchestrator", "hi")
    ended = _ended(await collect_until_turn_ended(stream))

    assert (ended.data.status, ended.data.error_kind) == ("failed", "keys_exhausted")
    assert "Provide a concise summary" in str(fake_openai.state.requests[-1]["messages"])
    assert await env.stalls(ended.data.turn_id) == []
    assert await env.event_payloads("turn.stalled") == []
    message = await env.agent_message(ended.data.turn_id)
    assert (message.status, message.error_kind) == ("complete", "keys_exhausted")
    await env.session.manager.close()


async def test_keys_running_out_after_text_fail_the_turn_without_a_stall(
    env: StallEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_after_text(monkeypatch, rate_limit())
    stream = await opened_stream(env.session)

    await env.session.manager.send(env.project_id, "orchestrator", "hi")
    ended = _ended(await collect_until_turn_ended(stream))

    assert (ended.data.status, ended.data.error_kind) == ("failed", "keys_exhausted")
    assert ended.data.error is not None
    assert ended.data.error.endswith("Key 1: rate limited (since 14:03 UTC).")
    assert KEY not in ended.data.error
    assert await env.stalls(ended.data.turn_id) == []
    message = await env.agent_message(ended.data.turn_id)
    assert message.parts == [TextPart(type="text", text="Partial ")]
    await env.session.manager.close()


async def test_a_tool_approval_and_a_stall_in_one_turn_do_not_interfere(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    write_tool = DummyWriteTool()
    env = await build_stall_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        tools=make_tool_reg(write_tool),
        extra_dirs=[write_agent_def(tmp_path)],
    )
    await env.session.settings.set_default_model("writer", f"{env.session.provider}:fake-tools")
    fake_openai.state.scripted_tool_calls = [
        {"name": "write_code_tool", "arguments": {"file_path": "a.py", "content": "print(1)"}}
    ]
    # The tool call is answered; the model step after the tool's result runs out of keys.
    fake_openai.state.script_for_key(KEY, ["ok", "401"])
    stream, _, turn_id = await send_until_waiting(env, "Please write a.py", agent="writer")
    [tool_approval] = await env.session.approval_store.pending(env.project_id)
    assert tool_approval.kind == "tool"

    await env.approvals.decide(env.project_id, tool_approval.id, approve=True, note=None)
    await collect_until_turn_waiting(stream)
    [stall] = await env.stalls(turn_id)
    await env.approvals.decide(env.project_id, stall.id, approve=True, note=None)
    ended = _ended(await collect_until_turn_ended(stream))

    assert ended.data.status == "done"
    assert len(write_tool.invoked) == 1
    message = await env.agent_message(turn_id, agent="writer")
    approvals = [(p.kind, p.approval_id, p.status) for p in message.parts if p.type == "approval"]
    assert approvals == [
        ("tool", tool_approval.id, "approved"),
        ("stall", stall.id, "approved"),
    ]
    await env.session.manager.close()
