"""A Stop that lands while the executor records a failure still marks the message stopped."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import httpx2
import pytest
from langchain_core.messages import BaseMessage

from mlops_agents.agents.runtime.contract import Part, TextPart, TurnEnded
from mlops_agents.agents.runtime.executor import TurnExecutor
from mlops_agents.agents.runtime.graph import TurnSink
from mlops_agents.agents.runtime.model_caller import ModelCaller, ModelReply, TurnFailed
from mlops_agents.paths import DataPaths
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.observer_fakes import RecordingObserver, turn_statuses
from tests.agents.runtime.session_env import (
    agent_message_statuses,
    build_session_env,
    collect_until_turn_ended,
    opened_stream,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

AGENT = "orchestrator"
PARTIAL_TEXT = "Partial text before the failure"


def fail_the_first_model_call(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    call = ModelCaller.call
    calls: list[int] = []

    async def failing_once(
        self: ModelCaller,
        model: ResolvedModel,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]],
        on_text: Callable[[str], Awaitable[None]],
    ) -> ModelReply:
        calls.append(1)
        if len(calls) == 1:
            await on_text(PARTIAL_TEXT)
            raise error
        return await call(self, model, messages, tools, on_text)

    monkeypatch.setattr(ModelCaller, "call", failing_once)


def hold_the_first_failure_record(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """The first failure record waits, before it writes anything, until the turn is cancelled."""
    record_failure = TurnExecutor.record_failure
    entered = asyncio.Event()

    async def held_once(
        self: TurnExecutor,
        project_id: str,
        session_id: str,
        turn_id: str,
        sink: TurnSink,
        lock: AbstractAsyncContextManager[Any],
        text: str,
        *,
        parts: list[Part] | None = None,
    ) -> None:
        if not entered.is_set():
            entered.set()
            await asyncio.Event().wait()
        await record_failure(self, project_id, session_id, turn_id, sink, lock, text, parts=parts)

    monkeypatch.setattr(TurnExecutor, "record_failure", held_once)
    return entered


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(TurnFailed("The model refused"), id="turn-failed"),
        pytest.param(RuntimeError("an unexpected error"), id="unexpected-error"),
    ],
)
async def test_a_stop_during_the_failure_record_marks_the_message_stopped(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    fail_the_first_model_call(monkeypatch, error)
    recording = hold_the_first_failure_record(monkeypatch)
    observer = RecordingObserver()
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        observers=[observer],
    )
    sub = await opened_stream(env)
    await env.manager.send(env.project_id, AGENT, "Check readme")
    await asyncio.wait_for(recording.wait(), timeout=10)
    queued = await env.manager.send(env.project_id, AGENT, "Check readme again")
    assert queued.status == "queued"
    await env.manager.stop(env.project_id, AGENT)

    first = (await collect_until_turn_ended(sub, timeout=10))[-1]
    second = (await collect_until_turn_ended(sub, timeout=10))[-1]

    assert isinstance(first, TurnEnded) and isinstance(second, TurnEnded)
    assert first.data.turn_id != second.data.turn_id
    assert (first.data.status, second.data.status) == ("stopped", "done")
    for _ in range(2):
        await asyncio.wait_for(observer.ended.get(), timeout=10)
    assert [(c[0], c[4]) for c in observer.calls if c[0] == "ended"] == [
        ("ended", "stopped"),
        ("ended", "done"),
    ]
    # The Stop shows as such, and the partial output is kept.
    assert await agent_message_statuses(env) == ["stopped", "complete"]
    session_id = await env.store.session_id(env.project_id, AGENT)
    stopped = await env.store.turn_agent_message(env.project_id, session_id, first.data.turn_id)
    assert stopped is not None
    assert TextPart(type="text", text=PARTIAL_TEXT) in stopped.parts
    assert sorted(await turn_statuses(databases, env.project_id)) == ["done", "stopped"]

    await env.manager.close()
