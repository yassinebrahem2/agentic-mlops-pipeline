"""Try again on a failed turn (Decision #400): a turn that failed with every key exhausted is
re-run as a new turn on the same trigger message, never re-sent as the user. A real session
stack on the fake server, one key."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import func, select

from mlops_agents.agents.runtime.contract import MessageView, StreamEvent, TurnEnded
from mlops_agents.errors import DomainError
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import turns
from tests.agents.runtime.caller_env import fail_after_text
from tests.agents.runtime.session_env import collect_until_turn_ended, opened_stream
from tests.agents.runtime.stall_env import StallEnv, build_stall_env
from tests.agents.runtime.test_resuming_turns import collect_until_turn_waiting
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake
from tests.providers.fake_clock import unauthorized

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
) -> AsyncGenerator[StallEnv, None]:
    built = await build_stall_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    yield built
    await built.session.manager.close()


def _ended(events: list[StreamEvent]) -> TurnEnded:
    return next(e for e in events if isinstance(e, TurnEnded))


async def _until_ended(stream: AsyncGenerator[StreamEvent, None]) -> TurnEnded:
    return _ended(await collect_until_turn_ended(stream))


async def _failed_turn(
    env: StallEnv, stream: AsyncGenerator[StreamEvent, None], *, from_agent: str | None = None
) -> str:
    """A turn that fails with its one key rejected after text streamed: `keys_exhausted`, the
    key marked rejected and the provider marked needs attention."""
    manager = env.session.manager
    with pytest.MonkeyPatch.context() as patch:
        fail_after_text(patch, unauthorized())
        if from_agent is None:
            await manager.send(env.project_id, AGENT, "Train the baseline")
        else:
            await manager.post(
                env.project_id,
                AGENT,
                author_name=from_agent,
                from_agent=from_agent,
                text="Please review a.py",
                exchange_id=None,
                hop=None,
            )
        ended = await _until_ended(stream)
    assert (ended.data.status, ended.data.error_kind) == ("failed", "keys_exhausted")
    return ended.data.turn_id


async def _done_turn(env: StallEnv, stream: AsyncGenerator[StreamEvent, None]) -> str:
    await env.session.manager.send(env.project_id, AGENT, "hi")
    ended = await _until_ended(stream)
    assert ended.data.status == "done"
    return ended.data.turn_id


async def _provider_status(env: StallEnv) -> str:
    return (await env.session.providers.get_provider(env.session.provider)).status


async def _messages(env: StallEnv) -> list[MessageView]:
    return (await env.session.manager.get(env.project_id, AGENT)).messages


async def _turn_count(env: StallEnv) -> int:
    session_id = await env.session.store.session_id(env.project_id, AGENT)
    engine = await env.databases.project(env.project_id)
    async with engine.connect() as conn:
        count = await conn.scalar(
            select(func.count()).select_from(turns).where(turns.c.session_id == session_id)
        )
    return int(count or 0)


async def _retry_refused(env: StallEnv, turn_id: str) -> DomainError:
    with pytest.raises(DomainError) as raised:
        await env.session.manager.retry_turn(env.project_id, AGENT, turn_id)
    return raised.value


async def test_retry_re_runs_the_trigger_as_a_new_turn_and_clears_the_key_marks(
    env: StallEnv, fake_openai: RunningFake
) -> None:
    stream = await opened_stream(env.session)
    failed = await _failed_turn(env, stream)
    trigger = await env.session.store.turn_trigger_id(env.project_id, failed)
    assert await _provider_status(env) == "needs_attention"
    requests_before = len(fake_openai.state.requests)

    await env.session.manager.retry_turn(env.project_id, AGENT, failed)
    ended = await _until_ended(stream)

    # Without the reset the rejected key would stall the new turn: it ends done.
    assert ended.data.status == "done"
    assert ended.data.turn_id != failed
    assert await env.session.store.turn_trigger_id(env.project_id, ended.data.turn_id) == trigger
    # The new turn asked the model about the same trigger.
    retried = fake_openai.state.requests[requests_before]["messages"]
    assert {"role": "user", "content": "Train the baseline"} in retried
    assert await _provider_status(env) == "ok"
    messages = await _messages(env)
    assert [(m.author_kind, m.turn_id) for m in messages] == [
        ("user", None),
        ("agent", failed),
        ("system", failed),  # the failure's text
        ("agent", ended.data.turn_id),
    ]
    assert messages[0].id == trigger


async def test_a_turn_another_agent_triggered_retries_with_that_message(
    env: StallEnv,
) -> None:
    stream = await opened_stream(env.session)
    failed = await _failed_turn(env, stream, from_agent="writer")
    trigger = await env.session.store.turn_trigger_id(env.project_id, failed)

    await env.session.manager.retry_turn(env.project_id, AGENT, failed)
    ended = await _until_ended(stream)

    assert ended.data.status == "done"
    assert await env.session.store.turn_trigger_id(env.project_id, ended.data.turn_id) == trigger
    [message] = [m for m in await _messages(env) if m.author_kind == "user"]
    assert (message.id, message.author_name, message.from_agent) == (trigger, "writer", "writer")


async def test_a_double_click_makes_one_turn_and_one_conflict(env: StallEnv) -> None:
    stream = await opened_stream(env.session)
    failed = await _failed_turn(env, stream)
    manager = env.session.manager

    results = await asyncio.gather(
        manager.retry_turn(env.project_id, AGENT, failed),
        manager.retry_turn(env.project_id, AGENT, failed),
        return_exceptions=True,
    )
    ended = await _until_ended(stream)

    assert ended.data.status == "done"
    refused = [r for r in results if isinstance(r, DomainError)]
    assert [r for r in results if r is None] == [None]
    assert [(r.code, r.status) for r in refused] == [("turn_not_retryable", 409)]
    assert await _turn_count(env) == 2


async def test_an_older_failed_turn_is_not_retried(env: StallEnv) -> None:
    stream = await opened_stream(env.session)
    failed = await _failed_turn(env, stream)
    # The next turn finds the key still rejected and stalls: a later turn, not ended.
    await env.session.manager.send(env.project_id, AGENT, "hi")
    await collect_until_turn_waiting(stream)

    refused = await _retry_refused(env, failed)

    assert (refused.code, refused.status) == ("turn_not_retryable", 409)
    assert await _turn_count(env) == 2


async def test_a_turn_that_failed_for_another_reason_is_not_retried(env: StallEnv) -> None:
    await env.session.settings.set_default_model(AGENT, None)
    stream = await opened_stream(env.session)
    await env.session.manager.send(env.project_id, AGENT, "hi")
    ended = await _until_ended(stream)
    assert (ended.data.status, ended.data.error_kind) == ("failed", None)

    refused = await _retry_refused(env, ended.data.turn_id)

    assert (refused.code, refused.status) == ("turn_not_retryable", 409)
    assert await _turn_count(env) == 1


async def test_a_done_turn_is_not_retried(env: StallEnv) -> None:
    stream = await opened_stream(env.session)
    done = await _done_turn(env, stream)

    refused = await _retry_refused(env, done)

    assert (refused.code, refused.status) == ("turn_not_retryable", 409)
    assert await _turn_count(env) == 1


async def test_an_unknown_turn_is_not_found(env: StallEnv) -> None:
    refused = await _retry_refused(env, "trn_01J00000000000000000000000")

    assert (refused.code, refused.status) == ("turn_not_found", 404)


async def test_a_turn_of_another_agents_session_is_not_found(env: StallEnv) -> None:
    stream = await opened_stream(env.session)
    failed = await _failed_turn(env, stream)
    manager = env.session.manager

    with pytest.raises(DomainError) as raised:
        await manager.retry_turn(env.project_id, "data-quality", failed)

    assert (raised.value.code, raised.value.status) == ("turn_not_found", 404)
