"""Why a turn failed (`error_kind`): stored on the turn, sent with turn.ended (the session stream
and the event log), and shown on the failed turn's agent message, live and after a reload."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
from sqlalchemy import select

from mlops_agents.agents.runtime.contract import (
    ErrorKind,
    MessageUpdated,
    MessageView,
    TurnEnded,
)
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.events.observer import EventTurnObserver
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import turns
from tests.agents.runtime.observer_fakes import RecordingObserver
from tests.agents.runtime.session_env import (
    SessionEnv,
    build_session_env,
    collect_until_turn_ended,
    opened_stream,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

KEY = "gsk_only_secret_value"


@dataclass(frozen=True)
class _Failed:
    """One failed turn, as every place that reports it saw it."""

    stored: ErrorKind | None  # turns.error_kind
    ended: TurnEnded  # the session stream's turn.ended
    live: list[MessageView]  # the agent message's message.updated events
    reloaded: list[MessageView]  # the turn's messages in the session view, read again
    logged: dict[str, Any]  # the event log's turn.ended payload


async def _fail_one_turn(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    *,
    exhaust_keys: bool,
) -> _Failed:
    event_log = SqliteEventLog(databases)
    recorder = RecordingObserver()
    env: SessionEnv = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake,
        model_http,
        git_env,
        # The recorder hears the end after the event log's observer has recorded it.
        observers=[EventTurnObserver(event_log), recorder],
    )
    await env.providers.add_key(env.provider, KEY)
    if exhaust_keys:
        fake.state.script_for_key(KEY, ["401"])
    else:
        fake.state.server_error_remaining = 3
    stream = await opened_stream(env)

    await env.manager.send(env.project_id, "orchestrator", "hi")
    events = await collect_until_turn_ended(stream)
    await recorder.ended.get()

    ended = next(e for e in events if isinstance(e, TurnEnded))
    engine = await databases.project(env.project_id)
    async with engine.connect() as conn:
        stored = (
            await conn.execute(select(turns.c.error_kind).where(turns.c.id == ended.data.turn_id))
        ).scalar_one()
    live = [
        e.data.message
        for e in events
        if isinstance(e, MessageUpdated) and e.data.message.author_kind == "agent"
    ]
    view = await env.manager.get(env.project_id, "orchestrator")
    reloaded = [m for m in view.messages if m.turn_id == ended.data.turn_id]
    logged = (await event_log.history(env.project_id, types=["turn.ended"]))[0].payload
    await env.manager.close()
    return _Failed(stored, ended, live, reloaded, logged)


async def test_a_turn_with_every_key_exhausted_reports_keys_exhausted_everywhere(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    failed = await _fail_one_turn(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        exhaust_keys=True,
    )

    assert failed.ended.data.status == "failed"
    assert failed.stored == "keys_exhausted"
    assert failed.ended.data.error_kind == "keys_exhausted"
    assert failed.logged["error_kind"] == "keys_exhausted"
    # The live update that completes the agent message already carries it.
    assert failed.live[-1].status == "complete"
    assert failed.live[-1].error_kind == "keys_exhausted"
    by_author = {m.author_kind: m for m in failed.reloaded}
    assert by_author["agent"].error_kind == "keys_exhausted"
    # The failure notice is not a second place to offer Try again.
    assert by_author["system"].error_kind is None
    assert KEY not in by_author["system"].parts[0].model_dump_json()


async def test_any_other_failure_leaves_error_kind_null(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    failed = await _fail_one_turn(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        exhaust_keys=False,
    )

    assert failed.ended.data.status == "failed"
    assert failed.stored is None
    assert failed.ended.data.error_kind is None
    assert failed.logged["error_kind"] is None
    assert failed.live[-1].error_kind is None
    assert [m.error_kind for m in failed.reloaded] == [None, None]
