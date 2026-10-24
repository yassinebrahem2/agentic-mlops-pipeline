"""update_message: a stored message's parts changed and published as one step under the lock."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.runtime.contract import (
    ApprovalPart,
    ApprovalStatus,
    MessageStatus,
    MessageUpdated,
    MessageView,
    Part,
    TextPart,
)
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.tools.base import Preview
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import SessionEnv, build_session_env
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

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


def _approval(approval_id: str, status: ApprovalStatus = "pending") -> ApprovalPart:
    return ApprovalPart(
        type="approval",
        approval_id=approval_id,
        tool_call_id=f"call_{approval_id}",
        preview=Preview(kind="command", title="Bash", body="ls"),
        status=status,
        note=None,
    )


def _decide(approval_id: str) -> Callable[[list[Part]], list[Part]]:
    def transform(parts: list[Part]) -> list[Part]:
        return [
            part.model_copy(update={"status": "approved"})
            if isinstance(part, ApprovalPart) and part.approval_id == approval_id
            else part
            for part in parts
        ]

    return transform


async def _agent_message(env: SessionEnv, parts: list[Part]) -> tuple[str, MessageView]:
    view = await env.manager.get(env.project_id, AGENT)
    message = await env.store.add_message(
        env.project_id,
        view.id,
        author_kind="agent",
        author_name=AGENT,
        parts=parts,
        status="waiting",
    )
    return view.id, message


def _hold_store_writes(
    monkeypatch: pytest.MonkeyPatch, *, after_write: bool
) -> tuple[asyncio.Event, asyncio.Event]:
    """Pause the first SessionStore.update_message, before or after its write."""
    orig = SessionStore.update_message
    entered = asyncio.Event()
    release = asyncio.Event()

    async def held(
        self: SessionStore,
        project_id: str,
        message_id: str,
        *,
        parts: Sequence[Part] | None = None,
        status: MessageStatus | None = None,
        read_by_turn: str | None = None,
    ) -> MessageView:
        first = not entered.is_set()
        entered.set()
        if first and not after_write:
            await release.wait()
        result = await orig(
            self, project_id, message_id, parts=parts, status=status, read_by_turn=read_by_turn
        )
        if first and after_write:
            await release.wait()
        return result

    monkeypatch.setattr(SessionStore, "update_message", held)
    return entered, release


async def test_update_message_stores_the_parts_and_publishes_one_message_updated(
    env: SessionEnv,
) -> None:
    manager = env.manager
    session_id, message = await _agent_message(
        env, [TextPart(type="text", text="I will run ls."), _approval("apr_1")]
    )
    before = await manager.get(env.project_id, AGENT)
    sub = manager.subscribe(env.project_id, AGENT, after_seq=before.last_seq)

    updated = await manager.update_message(env.project_id, session_id, message.id, _decide("apr_1"))

    expected = [TextPart(type="text", text="I will run ls."), _approval("apr_1", "approved")]
    assert updated.parts == expected
    assert updated.status == "waiting"
    event = await asyncio.wait_for(anext(sub), timeout=5)
    assert isinstance(event, MessageUpdated)
    assert event.data.message == updated
    after = await manager.get(env.project_id, AGENT)
    # The one event is the only thing published since the first view.
    assert after.last_seq == event.seq == before.last_seq + 1
    assert [m.parts for m in after.messages if m.id == message.id] == [expected]
    await sub.aclose()


async def test_get_during_update_message_sees_the_new_parts_and_not_the_event_again(
    env: SessionEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = env.manager
    session_id, message = await _agent_message(env, [_approval("apr_1"), _approval("apr_2")])
    entered, release = _hold_store_writes(monkeypatch, after_write=True)

    update = asyncio.create_task(
        manager.update_message(env.project_id, session_id, message.id, _decide("apr_1"))
    )
    await entered.wait()
    # The parts are written but not yet published: a get now must wait for the publish.
    get = asyncio.create_task(manager.get(env.project_id, AGENT))
    # Held by the lock, get cannot finish however long it is given; without it, it would.
    done, _ = await asyncio.wait([get], timeout=0.2)
    assert not done
    release.set()
    first = await update
    view = await get

    assert [m.parts for m in view.messages if m.id == message.id] == [first.parts]
    # Resuming from the view's position never replays the update it already shows.
    sub = manager.subscribe(env.project_id, AGENT, after_seq=view.last_seq)
    second = await manager.update_message(env.project_id, session_id, message.id, _decide("apr_2"))
    event = await asyncio.wait_for(anext(sub), timeout=5)
    assert isinstance(event, MessageUpdated)
    assert event.data.message == second
    await sub.aclose()


async def test_two_concurrent_updates_to_different_parts_both_land(
    env: SessionEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = env.manager
    session_id, message = await _agent_message(env, [_approval("apr_1"), _approval("apr_2")])
    entered, release = _hold_store_writes(monkeypatch, after_write=False)

    first = asyncio.create_task(
        manager.update_message(env.project_id, session_id, message.id, _decide("apr_1"))
    )
    await entered.wait()
    # The first update has read the parts and is about to write them.
    second = asyncio.create_task(
        manager.update_message(env.project_id, session_id, message.id, _decide("apr_2"))
    )
    # Held by the lock, the second cannot read the parts before the first has written them.
    done, _ = await asyncio.wait([second], timeout=0.2)
    assert not done
    release.set()
    await asyncio.gather(first, second)

    view = await manager.get(env.project_id, AGENT)
    assert [m.parts for m in view.messages if m.id == message.id] == [
        [_approval("apr_1", "approved"), _approval("apr_2", "approved")]
    ]


async def test_update_message_of_another_session_fails_and_publishes_nothing(
    env: SessionEnv,
) -> None:
    manager = env.manager
    _, message = await _agent_message(env, [_approval("apr_1")])
    other = await env.store.session_id(env.project_id, "another-agent")
    before = await manager.get(env.project_id, AGENT)

    with pytest.raises(ValueError, match=message.id):
        await manager.update_message(env.project_id, other, message.id, _decide("apr_1"))

    after = await manager.get(env.project_id, AGENT)
    assert after.last_seq == before.last_seq
    assert [m.parts for m in after.messages if m.id == message.id] == [[_approval("apr_1")]]
