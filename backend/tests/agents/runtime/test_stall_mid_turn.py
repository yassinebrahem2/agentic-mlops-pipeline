"""Keys running out after a model step's text streamed (the user's rule, 2026-10-28): the step's
text is taken back and the next key answers the same step, or the turn stalls and Try again
resumes that step. A real graph with a checkpointer, the fake server and a fake clock."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from mlops_agents.agents.runtime.contract import (
    ApprovalPart,
    MessageUpdated,
    StreamEvent,
    TextDelta,
    TextPart,
    TurnEnded,
)
from mlops_agents.paths import DataPaths
from mlops_agents.providers.bridge import KEYLESS
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.caller_env import ScriptedClient, script_builds
from tests.agents.runtime.session_env import collect_until_turn_ended, opened_stream
from tests.agents.runtime.stall_env import KEY, StallEnv, build_stall_env, send_until_waiting
from tests.agents.runtime.test_resuming_turns import (
    DummyWriteTool,
    collect_until_turn_waiting,
    make_tool_reg,
    write_agent_def,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.providers.fake_clock import FakeClock, bad_request, rate_limit, unauthorized

SECOND_KEY = "gsk_second_stall_secret"
PARTIAL = TextPart(type="text", text="Partial ")
STEP_0_TEXT = "I will write a.py now. "


def _ended(events: list[StreamEvent]) -> TurnEnded:
    return next(e for e in events if isinstance(e, TurnEnded))


def _texts(parts: list[object]) -> list[str]:
    return [p.text for p in parts if isinstance(p, TextPart)]


def _take_back(events: list[StreamEvent]) -> MessageUpdated:
    """The first update of the agent message after its partial text streamed."""
    streamed = next(
        i for i, e in enumerate(events) if isinstance(e, TextDelta) and e.data.text == "Partial "
    )
    return next(e for e in events[streamed:] if isinstance(e, MessageUpdated))


async def _env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    *,
    keys: tuple[str, ...] = (KEY,),
    clock: FakeClock | None = None,
    write_tool: DummyWriteTool | None = None,
) -> StallEnv:
    env = await build_stall_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake,
        model_http,
        git_env,
        keys=keys,
        clock=clock,
        tools=make_tool_reg(write_tool) if write_tool is not None else None,
        extra_dirs=[write_agent_def(tmp_path)] if write_tool is not None else None,
    )
    if write_tool is not None:
        await env.session.settings.set_default_model("writer", f"{env.session.provider}:fake-tools")
    return env


async def test_a_429_after_text_is_taken_back_and_the_next_key_completes_the_step(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    env = await _env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        keys=(KEY, SECOND_KEY),
        clock=clock,
    )
    built = script_builds(monkeypatch, [rate_limit()])
    stream = await opened_stream(env.session)

    await env.session.manager.send(env.project_id, "orchestrator", "hi")
    events = await collect_until_turn_ended(stream, timeout=10)

    ended = _ended(events)
    assert ended.data.status == "done"
    assert built[:2] == [0, 1]
    assert clock.sleeps == []
    # The live copy is reset at once: the update after the partial text no longer holds it.
    assert PARTIAL not in _take_back(events).data.message.parts
    message = await env.agent_message(ended.data.turn_id)
    assert PARTIAL not in message.parts
    assert await env.stalls(ended.data.turn_id) == []
    await env.session.manager.close()


async def test_keys_running_out_after_text_stall_and_try_again_resumes_that_step(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_tool = DummyWriteTool()
    env = await _env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        write_tool=write_tool,
    )
    fake_openai.state.scripted_tool_calls = [
        {"name": "write_code_tool", "arguments": {"file_path": "a.py", "content": "print(1)"}}
    ]
    # Step 0 calls the tool; step 1, after the tool's result, streams text and its key runs out.
    script_builds(monkeypatch, [None, unauthorized()])
    stream, _, turn_id = await send_until_waiting(env, "Please write a.py", agent="writer")
    [tool_approval] = await env.session.approval_store.pending(env.project_id)
    await env.approvals.decide(env.project_id, tool_approval.id, approve=True, note=None)
    events = await collect_until_turn_waiting(stream)

    [stall] = await env.stalls(turn_id)
    assert (stall.status, stall.step) == ("pending", 1)
    assert PARTIAL not in _take_back(events).data.message.parts
    message = await env.agent_message(turn_id, agent="writer")
    assert (message.status, _texts(list(message.parts))) == ("waiting", [])
    values = await env.checkpoint(turn_id)
    assert values is not None
    assert [m for m in values["messages"] if isinstance(m, AIMessage)][-1].tool_calls

    await env.approvals.decide(env.project_id, stall.id, approve=True, note=None)
    ended = _ended(await collect_until_turn_ended(stream, timeout=10))

    assert ended.data.status == "done"
    assert len(write_tool.invoked) == 1
    # The resumed step's request holds step 0's answer and no partial answer of step 1.
    last_request = fake_openai.state.requests[-1]["messages"]
    assert [m["role"] for m in last_request if m["role"] == "assistant"] == ["assistant"]
    assert last_request[-1]["role"] == "tool"
    message = await env.agent_message(turn_id, agent="writer")
    assert _texts(list(message.parts)) == ["Done. First line: File written successfully."]
    # Step 0's call, its approval and result are intact, then the stall card, then step 1.
    kinds = [p.kind if isinstance(p, ApprovalPart) else p.type for p in message.parts]
    assert kinds == ["tool_call", "tool", "tool_result", "stall", "text"]
    await env.session.manager.close()


async def test_a_take_back_keeps_the_text_of_the_steps_before_it(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_tool = DummyWriteTool()
    env = await _env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        write_tool=write_tool,
    )
    # Step 0 answers with text and a tool call; step 1 streams text and its key runs out.
    args = '{"file_path": "a.py", "content": "print(1)"}'
    step_0 = ScriptedClient(
        [
            AIMessageChunk(content=STEP_0_TEXT),
            AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {"name": "write_code_tool", "args": args, "id": "call_s0", "index": 0}
                ],
            ),
        ]
    )
    script_builds(monkeypatch, [step_0, unauthorized()])
    stream, _, turn_id = await send_until_waiting(env, "Please write a.py", agent="writer")
    [tool_approval] = await env.session.approval_store.pending(env.project_id)
    await env.approvals.decide(env.project_id, tool_approval.id, approve=True, note=None)
    events = await collect_until_turn_waiting(stream)

    [stall] = await env.stalls(turn_id)
    taken_back = _take_back(events).data.message
    assert _texts(list(taken_back.parts)) == [STEP_0_TEXT]
    message = await env.agent_message(turn_id, agent="writer")
    assert _texts(list(message.parts)) == [STEP_0_TEXT]

    await env.approvals.decide(env.project_id, stall.id, approve=True, note=None)
    ended = _ended(await collect_until_turn_ended(stream, timeout=10))

    assert ended.data.status == "done"
    assert len(write_tool.invoked) == 1
    message = await env.agent_message(turn_id, agent="writer")
    assert _texts(list(message.parts)) == [
        STEP_0_TEXT,
        "Done. First line: File written successfully.",
    ]
    await env.session.manager.close()


async def test_a_429_after_text_is_gone_before_the_wait_and_a_stop_in_the_wait_keeps_none(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    clock.hold = asyncio.Event()
    env = await _env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, clock=clock
    )
    script_builds(monkeypatch, [rate_limit("5")])
    stream = await opened_stream(env.session)

    await env.session.manager.send(env.project_id, "orchestrator", "hi")
    await asyncio.wait_for(clock.sleeping.wait(), timeout=10)
    # The session view of a busy turn is the hub's live copy, which holds the streamed text.
    view = await env.session.manager.get(env.project_id, "orchestrator")
    [waiting_message] = [m for m in view.messages if m.author_kind == "agent"]
    assert waiting_message.status == "streaming"
    assert PARTIAL not in waiting_message.parts

    await env.session.manager.stop(env.project_id, "orchestrator")
    ended = _ended(await collect_until_turn_ended(stream))

    assert ended.data.status == "stopped"
    assert clock.sleeps == [5.0]
    message = await env.agent_message(ended.data.turn_id)
    assert (message.status, PARTIAL in message.parts) == ("stopped", False)
    await env.session.manager.close()


async def test_a_non_key_failure_after_text_fails_the_turn_and_keeps_the_text(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = await _env(tmp_path, paths, databases, keystore, fake_openai, model_http, git_env)
    script_builds(monkeypatch, [bad_request()])
    stream = await opened_stream(env.session)

    await env.session.manager.send(env.project_id, "orchestrator", "hi")
    ended = _ended(await collect_until_turn_ended(stream))

    assert (ended.data.status, ended.data.error) == ("failed", "bad model")
    assert await env.stalls(ended.data.turn_id) == []
    message = await env.agent_message(ended.data.turn_id)
    assert message.parts == [PARTIAL]
    await env.session.manager.close()


@pytest.mark.parametrize("after_text", [False, True], ids=["before-text", "after-text"])
async def test_a_keyless_provider_running_out_fails_the_turn_without_a_stall(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    after_text: bool,
) -> None:
    env = await _env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, keys=()
    )
    if after_text:
        script_builds(monkeypatch, [unauthorized()])
    else:
        fake_openai.state.script_for_key(KEYLESS, ["401"])
    stream = await opened_stream(env.session)

    await env.session.manager.send(env.project_id, "orchestrator", "hi")
    ended = _ended(await collect_until_turn_ended(stream))

    assert ended.data.status == "failed"
    assert await env.stalls(ended.data.turn_id) == []
    assert await env.event_payloads("turn.stalled") == []
    message = await env.agent_message(ended.data.turn_id)
    assert message.parts == ([PARTIAL] if after_text else [])
    await env.session.manager.close()
