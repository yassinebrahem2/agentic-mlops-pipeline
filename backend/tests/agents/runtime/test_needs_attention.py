"""A provider marked needs attention only informs (Decision #384): its turns still reach the
model, a success or Try again clears the mark, and a call that fails leaves it."""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from langchain_core.messages import BaseMessage, HumanMessage

from mlops_agents.agents.runtime.contract import TurnEnded
from mlops_agents.agents.runtime.model_caller import TurnFailed
from mlops_agents.paths import DataPaths
from mlops_agents.providers.models import Provider
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.caller_env import CallerEnv, build_caller_env
from tests.agents.runtime.session_env import (
    build_session_env,
    collect_until_turn_ended,
    opened_stream,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

KEYS = ("gsk_first_secret_value", "gsk_second_secret_value")
HI: list[BaseMessage] = [HumanMessage(content="hi")]
DETAIL = "groq: no usable key for fake-no-tools. Key 1: rejected by the provider."


async def _noop(_: str) -> None:
    pass


@pytest.fixture
async def env(
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
) -> CallerEnv:
    return await build_caller_env(
        databases,
        keystore,
        fake_openai,
        model_http,
        name="groq",
        wire_model_ids=["fake-no-tools"],
        keys=KEYS,
    )


async def _provider(env: CallerEnv) -> Provider:
    return await env.providers.get_provider_by_id(env.model.provider_id)


async def test_a_successful_call_clears_the_mark_and_a_healthy_provider_is_not_written(
    env: CallerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_attention = env.providers.clear_attention
    clears: list[str] = []

    async def counting(provider_id: str) -> None:
        clears.append(provider_id)
        await clear_attention(provider_id)

    monkeypatch.setattr(env.providers, "clear_attention", counting)
    await env.caller.call(env.model, HI, [], _noop)
    assert clears == []

    await env.providers.mark_needs_attention(env.model.provider_id, DETAIL)
    await env.caller.call(env.model, HI, [], _noop)

    assert clears == [env.model.provider_id]
    provider = await _provider(env)
    assert (provider.status, provider.status_detail) == ("ok", None)


async def test_try_again_clears_the_mark(env: CallerEnv) -> None:
    await env.providers.mark_needs_attention(env.model.provider_id, DETAIL)

    await env.caller.reset_keys(env.model.provider_id)

    provider = await _provider(env)
    assert (provider.status, provider.status_detail) == ("ok", None)


async def test_a_call_that_fails_leaves_the_mark(env: CallerEnv, fake_openai: RunningFake) -> None:
    await env.providers.mark_needs_attention(env.model.provider_id, DETAIL)
    fake_openai.state.server_error_remaining = 3

    with pytest.raises(TurnFailed):
        await env.caller.call(env.model, HI, [], _noop)

    provider = await _provider(env)
    assert (provider.status, provider.status_detail) == ("needs_attention", DETAIL)


async def test_a_provider_needing_attention_still_starts_a_turn_that_reaches_the_model(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    session = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    provider = await session.providers.get_provider(session.provider)
    await session.providers.mark_needs_attention(provider.id, DETAIL)
    fake_openai.state.requests.clear()
    stream = await opened_stream(session)

    await session.manager.send(session.project_id, "orchestrator", "hi")
    events = await collect_until_turn_ended(stream, timeout=10)

    ended = events[-1]
    assert isinstance(ended, TurnEnded)
    assert ended.data.status == "done"
    assert fake_openai.state.requests != []
    assert (await session.providers.get_provider(session.provider)).status == "ok"
    await session.manager.close()
