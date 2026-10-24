"""The session manager's turn observers and its binding to the message bus."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import AgentSessions, TurnEnd, TurnEnded
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import build_session_env, collect_until_turn_ended
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.stream_helpers import through_first_delta

AGENT = "orchestrator"


class RecordingObserver:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.ended: asyncio.Queue[TurnEnd] = asyncio.Queue()
        self._fail = fail

    async def turn_started(self, project_id: str, agent: str, turn_id: str) -> None:
        self.calls.append(("started", project_id, agent, turn_id))
        if self._fail:
            raise RuntimeError("observer failed")

    async def turn_waiting(self, project_id: str, agent: str, turn_id: str) -> None:
        self.calls.append(("waiting", project_id, agent, turn_id))

    async def turn_ended(
        self,
        project_id: str,
        agent: str,
        turn_id: str,
        status: TurnEnd,
        final_text: str | None,
    ) -> None:
        self.calls.append(("ended", project_id, agent, turn_id, status, str(final_text)))
        self.ended.put_nowait(status)
        if self._fail:
            raise RuntimeError("observer failed")


class FakeBus:
    """Binds once, as MessageBus says; a second bind raises."""

    def __init__(self) -> None:
        self.sessions: AgentSessions | None = None

    async def send(
        self, project_id: str, *, sender: str, sender_turn_id: str, to: str, text: str
    ) -> None:
        raise AssertionError("not used here")

    def bind(self, sessions: AgentSessions) -> None:
        if self.sessions is not None:
            raise RuntimeError("The message bus is already bound.")
        self.sessions = sessions


async def test_an_observer_sees_a_turn_start_and_end_with_its_final_text(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
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
    manager = env.manager
    view = await manager.get(env.project_id, AGENT)
    sub = manager.subscribe(env.project_id, AGENT, after_seq=view.last_seq)

    await manager.send(env.project_id, AGENT, "Check readme")
    events = await collect_until_turn_ended(sub)
    ended = events[-1]
    assert isinstance(ended, TurnEnded)
    assert await asyncio.wait_for(observer.ended.get(), timeout=5) == "done"

    turn_id = ended.data.turn_id
    assert observer.calls == [
        ("started", env.project_id, AGENT, turn_id),
        ("ended", env.project_id, AGENT, turn_id, "done", "Done. First line: 1\tLine 1 of readme"),
    ]

    await manager.close()


async def test_an_observer_sees_a_stopped_turn_end(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
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
    manager = env.manager
    await env.settings.set_default_model(AGENT, f"{env.provider}:fake-slow")
    fake_openai.state.hold_slow()
    sub = manager.subscribe(env.project_id, AGENT, after_seq=None)
    await anext(sub)  # the opening resync

    await manager.send(env.project_id, AGENT, "Slow response")
    await through_first_delta(sub)
    await manager.stop(env.project_id, AGENT)
    await collect_until_turn_ended(sub)

    assert await asyncio.wait_for(observer.ended.get(), timeout=5) == "stopped"
    assert [call[0] for call in observer.calls] == ["started", "ended"]

    fake_openai.state.release_slow()
    await manager.close()


async def test_a_failing_observer_leaves_the_turns_running(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing = RecordingObserver(fail=True)
    after = RecordingObserver()
    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        observers=[failing, after],
    )
    manager = env.manager
    view = await manager.get(env.project_id, AGENT)
    sub = manager.subscribe(env.project_id, AGENT, after_seq=view.last_seq)

    await manager.send(env.project_id, AGENT, "First")
    first = (await collect_until_turn_ended(sub))[-1]
    await manager.send(env.project_id, AGENT, "Second")
    second = (await collect_until_turn_ended(sub))[-1]

    assert isinstance(first, TurnEnded) and first.data.status == "done"
    assert isinstance(second, TurnEnded) and second.data.status == "done"
    # The observer after the failing one is still called, for both turns.
    for _ in range(2):
        assert await asyncio.wait_for(after.ended.get(), timeout=5) == "done"
    assert [call[0] for call in after.calls] == ["started", "ended", "started", "ended"]
    assert "A turn observer failed" in caplog.text

    await manager.close()


async def test_the_session_manager_binds_itself_to_the_bus_once(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    bus = FakeBus()
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, bus=bus
    )

    assert bus.sessions is env.manager
    with pytest.raises(RuntimeError, match="already bound"):
        await build_session_env(
            tmp_path / "second",
            paths,
            databases,
            keystore,
            fake_openai,
            model_http,
            git_env,
            bus=bus,
        )

    await env.manager.close()
