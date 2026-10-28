"""When the text a model step streamed is taken back (Decision #410): only when a next attempt
starts, so a wait round still shows it, a Stop keeps it, a failure with no next attempt keeps
it, and text taken back never returns. A real graph with a checkpointer, the fake server and a
fake clock."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import MessageUpdated, StreamEvent
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.caller_env import ScriptedClient, script_builds
from tests.agents.runtime.session_env import collect_until_turn_ended, opened_stream
from tests.agents.runtime.stall_env import KEY, StallEnv
from tests.agents.runtime.test_stall_mid_turn import (
    PARTIAL,
    SECOND_KEY,
    ended_event,
    mid_turn_env,
    take_back_update,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.providers.fake_clock import FakeClock, bad_request, rate_limit


async def _held_in_a_wait_round(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[StallEnv, FakeClock, AsyncGenerator[StreamEvent, None]]:
    """The only key streams text, is rate limited for 5 s, and the turn sleeps until the test
    releases `clock.hold`; the live copy then still shows the text."""
    clock = FakeClock()
    clock.hold = asyncio.Event()
    env = await mid_turn_env(
        tmp_path, paths, databases, keystore, fake, model_http, git_env, clock=clock
    )
    script_builds(monkeypatch, [rate_limit("5")])
    stream = await opened_stream(env.session)
    await env.session.manager.send(env.project_id, "orchestrator", "hi")
    await asyncio.wait_for(clock.sleeping.wait(), timeout=10)
    # The session view of a busy turn is the hub's live copy, which holds the streamed text.
    view = await env.session.manager.get(env.project_id, "orchestrator")
    [waiting_message] = [m for m in view.messages if m.author_kind == "agent"]
    assert (waiting_message.status, waiting_message.parts) == ("streaming", [PARTIAL])
    return env, clock, stream


async def test_a_wait_round_shows_the_text_until_the_next_attempt_takes_it_back(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, clock, stream = await _held_in_a_wait_round(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, monkeypatch
    )
    assert clock.hold is not None

    clock.hold.set()
    events = await collect_until_turn_ended(stream, timeout=10)

    assert ended_event(events).data.status == "done"
    assert clock.sleeps == [5.0]
    assert PARTIAL not in take_back_update(events).data.message.parts
    assert PARTIAL not in (await env.agent_message(ended_event(events).data.turn_id)).parts
    await env.session.manager.close()


async def test_a_stop_during_a_wait_round_keeps_the_text(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, clock, stream = await _held_in_a_wait_round(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, monkeypatch
    )

    await env.session.manager.stop(env.project_id, "orchestrator")
    ended = ended_event(await collect_until_turn_ended(stream))

    assert ended.data.status == "stopped"
    assert clock.sleeps == [5.0]
    message = await env.agent_message(ended.data.turn_id)
    assert (message.status, message.parts) == ("stopped", [PARTIAL])
    await env.session.manager.close()


async def test_a_400_after_text_fails_the_turn_and_no_update_removes_the_text(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = await mid_turn_env(tmp_path, paths, databases, keystore, fake_openai, model_http, git_env)
    script_builds(monkeypatch, [bad_request()])
    stream = await opened_stream(env.session)

    await env.session.manager.send(env.project_id, "orchestrator", "hi")
    events = await collect_until_turn_ended(stream)

    ended = ended_event(events)
    assert (ended.data.status, ended.data.error) == ("failed", "bad model")
    assert await env.stalls(ended.data.turn_id) == []
    message = await env.agent_message(ended.data.turn_id)
    assert message.parts == [PARTIAL]
    # No attempt follows the 400, so nothing takes the text back, even for a moment.
    updates = [
        e.data.message.parts
        for e in events
        if isinstance(e, MessageUpdated) and e.data.message.id == message.id
    ]
    assert updates
    assert all(PARTIAL in parts for parts in updates)
    await env.session.manager.close()


async def test_text_taken_back_does_not_return_when_the_next_key_fails_before_text(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = await mid_turn_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        keys=(KEY, SECOND_KEY),
    )
    # Key 0 streams text and is rate limited; key 1 answers a 400 before any text.
    built = script_builds(monkeypatch, [rate_limit(), ScriptedClient([], bad_request())])
    stream = await opened_stream(env.session)

    await env.session.manager.send(env.project_id, "orchestrator", "hi")
    ended = ended_event(await collect_until_turn_ended(stream))

    assert (ended.data.status, ended.data.error) == ("failed", "bad model")
    assert built == [0, 1]
    message = await env.agent_message(ended.data.turn_id)
    assert message.parts == []
    await env.session.manager.close()
