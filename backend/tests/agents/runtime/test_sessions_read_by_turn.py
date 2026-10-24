"""A turn started by an agent's message records that it read it (messaging's exchange rule)."""

from __future__ import annotations

from mlops_agents.agents.runtime.contract import TurnEnded
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import SessionEnv, collect_until_turn_ended
from tests.agents.runtime.session_rows import create_exchange, message_row

AGENT = "orchestrator"


async def test_a_turn_started_by_an_agent_message_records_that_it_read_it(
    env: SessionEnv, databases: Databases
) -> None:
    manager = env.manager
    view = await manager.get(env.project_id, AGENT)
    sub = manager.subscribe(env.project_id, AGENT, after_seq=view.last_seq)
    await create_exchange(databases, env.project_id, "xch_01")

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

    row = await message_row(databases, env.project_id, posted.id)
    assert row["read_by_turn"] == ended.data.turn_id


async def test_a_turn_started_by_a_user_message_records_no_reader(
    env: SessionEnv, databases: Databases
) -> None:
    manager = env.manager
    view = await manager.get(env.project_id, AGENT)
    sub = manager.subscribe(env.project_id, AGENT, after_seq=view.last_seq)

    sent = await manager.send(env.project_id, AGENT, "Check readme")
    await collect_until_turn_ended(sub)

    row = await message_row(databases, env.project_id, sent.id)
    assert row["read_by_turn"] is None
