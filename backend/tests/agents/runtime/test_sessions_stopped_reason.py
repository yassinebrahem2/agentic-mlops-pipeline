from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from sqlalchemy import select

from mlops_agents.agents.runtime.contract import TurnEnded
from mlops_agents.agents.settings import AgentLimitsUpdate
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import turns
from tests.agents.runtime.session_env import (
    build_session_env,
    collect_until_turn_ended,
    opened_stream,
)
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


@pytest.mark.asyncio
async def test_turn_stopped_by_step_limit_carries_stopped_reason(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    manager = env.manager
    project_id = env.project_id
    agent = "orchestrator"

    # Step limit = 1: model node will stop immediately after step limit check
    await env.settings.set_project_limits(project_id, agent, AgentLimitsUpdate(max_steps=1))

    stream = await opened_stream(env, agent=agent)
    await manager.send(project_id, agent, "Run something")
    events = await collect_until_turn_ended(stream)

    ended_events = [e for e in events if isinstance(e, TurnEnded)]
    assert len(ended_events) == 1
    turn_ended = ended_events[0]
    assert turn_ended.data.status == "done"
    assert turn_ended.data.stopped_reason == "max_steps"

    # Verify stored turn has stopped_reason = 'max_steps'
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(turns.c.status, turns.c.stopped_reason).where(
                    turns.c.id == turn_ended.data.turn_id
                )
            )
        ).first()
        assert row is not None
        assert row[0] == "done"
        assert row[1] == "max_steps"

    # Verify SessionView has last_stopped_reason == "max_steps"
    view = await manager.get(project_id, agent)
    assert view.last_stopped_reason == "max_steps"

    # Reset step limit to default (no limit) and send another message
    await env.settings.set_project_limits(project_id, agent, AgentLimitsUpdate(max_steps=None))
    stream2 = await opened_stream(env, agent=agent)
    await manager.send(project_id, agent, "Hello normal turn")
    events2 = await collect_until_turn_ended(stream2)

    ended_events2 = [e for e in events2 if isinstance(e, TurnEnded)]
    assert len(ended_events2) == 1
    turn_ended2 = ended_events2[0]
    assert turn_ended2.data.status == "done"
    assert turn_ended2.data.stopped_reason is None

    # Verify stored turn has stopped_reason is None
    async with engine.connect() as conn:
        row2 = (
            await conn.execute(
                select(turns.c.status, turns.c.stopped_reason).where(
                    turns.c.id == turn_ended2.data.turn_id
                )
            )
        ).first()
        assert row2 is not None
        assert row2[0] == "done"
        assert row2[1] is None

    # Verify SessionView now has last_stopped_reason is None
    view2 = await manager.get(project_id, agent)
    assert view2.last_stopped_reason is None

    await manager.close()
