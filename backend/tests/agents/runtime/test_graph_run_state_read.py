"""A waiting turn is never left without its card: when the interrupted graph's state cannot be
read, the turn fails, with the error logged at warning."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx2
import pytest
from langgraph.graph.state import CompiledStateGraph  # pyright: ignore[reportMissingTypeStubs]

from mlops_agents.agents.runtime.contract import TurnEnded, TurnWaiting
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import collect_until_turn_ended, opened_stream, until_idle
from tests.agents.runtime.stall_env import KEY, build_stall_env
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


async def test_a_failed_state_read_after_a_stall_fails_the_turn_and_warns(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = await build_stall_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    fake_openai.state.script_for_key(KEY, ["401"])

    async def unreadable(self: object, *args: object, **kwargs: object) -> object:
        raise RuntimeError("state unreadable")

    monkeypatch.setattr(CompiledStateGraph, "aget_state", unreadable)
    stream = await opened_stream(env.session)
    await env.session.manager.send(env.project_id, "orchestrator", "hi")
    events = await collect_until_turn_ended(stream, timeout=5)

    ended = next(e for e in events if isinstance(e, TurnEnded))
    assert ended.data.status == "failed"
    assert not any(isinstance(e, TurnWaiting) for e in events)
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and ended.data.turn_id in r.getMessage()
    ]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None
    assert await env.checkpoint(ended.data.turn_id) is None
    # The end is published before the sweep that settles the turn's approvals runs.
    await until_idle(env.session.manager, env.project_id, "orchestrator")
    assert [s.status for s in await env.stalls(ended.data.turn_id)] == ["cancelled"]
    await env.session.manager.close()
