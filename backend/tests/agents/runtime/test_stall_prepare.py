"""Compaction stalls too (the user's rule, 2026-10-28): it runs in the turn graph's first node,
`prepare`, so keys running out there pause the turn on a stall at step 0, and Try again runs
`prepare` again. A real graph with a checkpointer, the fake server and a fake clock."""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import TextPart, TurnEnded
from mlops_agents.agents.runtime.stall import StallState
from mlops_agents.paths import DataPaths
from mlops_agents.providers.bridge import KEYLESS
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import collect_until_turn_ended, opened_stream
from tests.agents.runtime.stall_env import (
    KEY,
    StallEnv,
    build_stall_env,
    send_until_waiting,
    stall_parts,
)
from tests.agents.runtime.test_resuming_turns import collect_until_turn_waiting
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

SUMMARY_PROMPT = "Provide a concise summary"


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


async def add_long_history(env: StallEnv, agent: str = "orchestrator") -> None:
    """Three earlier turns past 75% of the model's context window: the next turn compacts."""
    session_id = await env.session.store.session_id(env.project_id, agent)
    for _ in range(3):
        await env.session.store.add_message(
            env.project_id,
            session_id,
            author_kind="user",
            author_name="user",
            parts=[TextPart(type="text", text="x" * 40_000)],
            status="complete",
        )


def _is_compaction(request: dict[str, object]) -> bool:
    return SUMMARY_PROMPT in str(request["messages"])


async def test_compaction_out_of_keys_stalls_at_step_0_and_try_again_compacts(
    env: StallEnv, fake_openai: RunningFake
) -> None:
    await add_long_history(env)
    fake_openai.state.script_for_key(KEY, ["401"])
    fake_openai.state.requests.clear()  # the setup's model verifications

    stream, _, turn_id = await send_until_waiting(env, "hi")

    assert [_is_compaction(r) for r in fake_openai.state.requests] == [True]
    [stall] = await env.stalls(turn_id)
    assert (stall.status, stall.step) == ("pending", 0)
    assert len(await env.event_payloads("turn.stalled")) == 1
    message = await env.agent_message(turn_id)
    assert (message.status, [p.type for p in message.parts]) == ("waiting", ["approval"])
    values = await env.checkpoint(turn_id)
    assert values is not None
    checkpointed = values["stall"]
    assert isinstance(checkpointed, StallState)
    assert (checkpointed.node, checkpointed.step) == ("prepare", 0)
    assert not values.get("messages")

    await env.approvals.decide(env.project_id, stall.id, approve=True, note=None)
    ended = next(e for e in await collect_until_turn_ended(stream) if isinstance(e, TurnEnded))

    assert ended.data.status == "done"
    requests = fake_openai.state.requests
    assert [_is_compaction(r) for r in requests[:3]] == [True, True, False]
    session_id = await env.session.store.session_id(env.project_id, "orchestrator")
    summary, _ = await env.session.store.summary(env.project_id, session_id)
    assert summary == "Hello from the fake model."
    # The model's first request is built after compaction, from its summary.
    assert summary in str(requests[2]["messages"][0]["content"])
    message = await env.agent_message(turn_id)
    assert message.status == "complete"
    assert [(p.approval_id, p.status) for p in stall_parts(message)] == [(stall.id, "approved")]
    await env.session.manager.close()


async def test_a_compaction_stall_then_a_model_stall_make_two_approvals(
    env: StallEnv, fake_openai: RunningFake
) -> None:
    await add_long_history(env)
    # Compaction runs out; after Try again it succeeds and the model step runs out.
    fake_openai.state.script_for_key(KEY, ["401", "ok", "401"])
    stream, _, turn_id = await send_until_waiting(env, "hi")
    [first] = await env.stalls(turn_id)

    await env.approvals.decide(env.project_id, first.id, approve=True, note=None)
    await collect_until_turn_waiting(stream)

    decided, second = await env.stalls(turn_id)
    assert (decided.id, decided.status) == (first.id, "approved")
    assert (second.status, second.step) == ("pending", 0)
    assert second.id != first.id
    values = await env.checkpoint(turn_id)
    assert values is not None
    assert values["stall"].node == "model"

    await env.approvals.decide(env.project_id, second.id, approve=True, note=None)
    ended = next(e for e in await collect_until_turn_ended(stream) if isinstance(e, TurnEnded))
    assert ended.data.status == "done"
    assert [s.status for s in await env.stalls(turn_id)] == ["approved", "approved"]
    await env.session.manager.close()


async def test_a_keyless_provider_failing_compaction_fails_the_turn_without_a_stall(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    # No key to rotate or fix (Decision #384): the provider's own message fails the turn.
    env = await build_stall_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, keys=()
    )
    await add_long_history(env)
    fake_openai.state.script_for_key(KEYLESS, ["401"])
    stream = await opened_stream(env.session)

    await env.session.manager.send(env.project_id, "orchestrator", "hi")
    ended = next(e for e in await collect_until_turn_ended(stream) if isinstance(e, TurnEnded))

    assert ended.data.status == "failed"
    assert ended.data.error is not None
    assert "Invalid API key" in ended.data.error
    assert _is_compaction(fake_openai.state.requests[-1])
    assert await env.stalls(ended.data.turn_id) == []
    assert await env.event_payloads("turn.stalled") == []
    message = await env.agent_message(ended.data.turn_id)
    assert (message.status, message.parts) == ("complete", [])
    assert await env.checkpoint(ended.data.turn_id) is None
    await env.session.manager.close()
