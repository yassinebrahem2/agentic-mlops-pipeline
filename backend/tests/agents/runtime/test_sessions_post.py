"""post (an inbound message from another agent) and the one busy rule."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import RowMapping, select

from mlops_agents.agents.runtime.contract import MessageCreated, TextPart, TurnEnded
from mlops_agents.agents.runtime.hub import AsyncRLock, SessionHub
from mlops_agents.agents.runtime.worker import TurnWorkers
from mlops_agents.errors import DomainError
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import messages, turns
from tests.agents.runtime.session_env import (
    SessionEnv,
    build_session_env,
    collect_until_turn_ended,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.stream_helpers import through_first_delta

AGENT = "orchestrator"


@pytest.fixture
async def env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> AsyncIterator[SessionEnv]:
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    yield env
    await env.manager.close()


async def _message_row(databases: Databases, project_id: str, message_id: str) -> RowMapping:
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        row = (
            (await conn.execute(select(messages).where(messages.c.id == message_id)))
            .mappings()
            .one()
        )
    return row


async def _turn_statuses(databases: Databases, project_id: str) -> dict[str, str]:
    """Each turn's status, keyed by its trigger message."""
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (await conn.execute(select(turns.c.trigger_message_id, turns.c.status))).all()
    return {str(r[0]): str(r[1]) for r in rows}


async def test_post_when_idle_stores_an_inbound_message_and_starts_a_turn(
    env: SessionEnv, databases: Databases
) -> None:
    manager = env.manager
    view = await manager.get(env.project_id, AGENT)
    sub = manager.subscribe(env.project_id, AGENT, after_seq=view.last_seq)

    posted = await manager.post(
        env.project_id,
        AGENT,
        author_name="Data Quality",
        from_agent="data-quality",
        text="Please look at the readme.",
        exchange_id="xch_01",
        hop=2,
    )

    assert posted.author_kind == "user"
    assert posted.author_name == "Data Quality"
    assert posted.from_agent == "data-quality"
    assert posted.status == "complete"
    assert posted.parts == [TextPart(type="text", text="Please look at the readme.")]

    events = await collect_until_turn_ended(sub)
    first = events[0]
    assert isinstance(first, MessageCreated)
    assert first.data.message == posted
    ended = events[-1]
    assert isinstance(ended, TurnEnded)
    assert ended.data.status == "done"

    row = await _message_row(databases, env.project_id, posted.id)
    assert row["exchange_id"] == "xch_01"
    assert row["hop"] == 2
    assert await _turn_statuses(databases, env.project_id) == {posted.id: "done"}


async def test_post_when_busy_queues_its_turn_like_send(
    env: SessionEnv, databases: Databases, fake_openai: RunningFake
) -> None:
    manager = env.manager
    await env.settings.set_default_model(AGENT, f"{env.provider}:fake-slow")
    fake_openai.state.hold_slow()
    sub = manager.subscribe(env.project_id, AGENT, after_seq=None)
    await anext(sub)  # the opening resync

    sent = await manager.send(env.project_id, AGENT, "First")
    await through_first_delta(sub)
    posted = await manager.post(
        env.project_id,
        AGENT,
        author_name="Data Quality",
        from_agent="data-quality",
        text="Second",
        exchange_id=None,
        hop=None,
    )

    assert sent.status == "complete"
    assert posted.status == "queued"
    assert (await manager.get(env.project_id, AGENT)).busy is True
    assert await _turn_statuses(databases, env.project_id) == {
        sent.id: "running",
        posted.id: "queued",
    }
    row = await _message_row(databases, env.project_id, posted.id)
    assert row["exchange_id"] is None
    assert row["hop"] is None

    await env.settings.set_default_model(AGENT, f"{env.provider}:fake-tools")
    fake_openai.state.release_slow()
    await collect_until_turn_ended(sub, timeout=10)
    await collect_until_turn_ended(sub, timeout=10)

    assert await _turn_statuses(databases, env.project_id) == {
        sent.id: "done",
        posted.id: "done",
    }


async def test_post_rejects_an_empty_message_like_send(env: SessionEnv) -> None:
    with pytest.raises(DomainError) as caught:
        await env.manager.post(
            env.project_id,
            AGENT,
            author_name="Data Quality",
            from_agent="data-quality",
            text="  ",
            exchange_id=None,
            hop=None,
        )
    assert caught.value.code == "message_empty"


async def test_a_turn_started_by_an_agent_message_records_that_it_read_it(
    env: SessionEnv, databases: Databases
) -> None:
    manager = env.manager
    view = await manager.get(env.project_id, AGENT)
    sub = manager.subscribe(env.project_id, AGENT, after_seq=view.last_seq)

    posted = await manager.post(
        env.project_id,
        AGENT,
        author_name="Data Quality",
        from_agent="data-quality",
        text="Please look at the readme.",
        exchange_id="xch_01",
        hop=1,
    )
    events = await collect_until_turn_ended(sub)
    ended = events[-1]
    assert isinstance(ended, TurnEnded)

    row = await _message_row(databases, env.project_id, posted.id)
    assert row["read_by_turn"] == ended.data.turn_id


async def test_a_turn_started_by_a_user_message_records_no_reader(
    env: SessionEnv, databases: Databases
) -> None:
    manager = env.manager
    view = await manager.get(env.project_id, AGENT)
    sub = manager.subscribe(env.project_id, AGENT, after_seq=view.last_seq)

    sent = await manager.send(env.project_id, AGENT, "Check readme")
    await collect_until_turn_ended(sub)

    row = await _message_row(databases, env.project_id, sent.id)
    assert row["read_by_turn"] is None


async def test_a_waiting_turn_makes_the_session_busy_and_holds_the_queue(
    env: SessionEnv, databases: Databases, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = env.manager
    store = env.store
    view = await manager.get(env.project_id, AGENT)
    trigger = await store.add_message(
        env.project_id,
        view.id,
        author_kind="user",
        author_name="user",
        parts=[TextPart(type="text", text="Write the file")],
        status="complete",
    )
    await store.add_turn(env.project_id, view.id, trigger.id, "waiting")

    started: list[tuple[str, str]] = []
    orig_start = TurnWorkers.start

    def recording_start(
        self: TurnWorkers,
        project_id: str,
        session_id: str,
        agent: str,
        hub: SessionHub,
        lock: AsyncRLock,
    ) -> None:
        started.append((project_id, session_id))
        orig_start(self, project_id, session_id, agent, hub, lock)

    monkeypatch.setattr(TurnWorkers, "start", recording_start)

    assert (await manager.get(env.project_id, AGENT)).busy is True
    sent = await manager.send(env.project_id, AGENT, "Behind the waiting turn")
    posted = await manager.post(
        env.project_id,
        AGENT,
        author_name="Data Quality",
        from_agent="data-quality",
        text="Also behind it",
        exchange_id=None,
        hop=None,
    )
    view_after = await manager.get(env.project_id, AGENT)

    assert sent.status == "queued"
    assert posted.status == "queued"
    assert view_after.busy is True
    assert started == []
    assert await _turn_statuses(databases, env.project_id) == {
        trigger.id: "waiting",
        sent.id: "queued",
        posted.id: "queued",
    }
