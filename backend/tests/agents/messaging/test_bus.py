"""SendMessage's behavior: delivery, exchanges and hops, limits, who may be messaged."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, insert, select

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.messaging.contract import MessageLimitReached
from mlops_agents.agents.messaging.settings import MessagingLimits
from mlops_agents.errors import DomainError
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import exchanges, llm_calls
from tests.agents.messaging.bus_env import BusEnv
from tests.agents.runtime.session_env import collect_until_turn_ended
from tests.agents.runtime.session_rows import message_row

NO_TURN = "trn_none"


async def deliver(env: BusEnv, *, sender: str, turn: str, to: str, text: str = "Hi") -> str:
    """Send, wait for the recipient's turn to end, and return the id of that turn."""
    manager = env.session.manager
    project_id = env.session.project_id
    view = await manager.get(project_id, to)
    sub = manager.subscribe(project_id, to, after_seq=view.last_seq)
    await env.bus.send(project_id, sender=sender, sender_turn_id=turn, to=to, text=text)
    events = await collect_until_turn_ended(sub)
    return events[-1].data.turn_id  # pyright: ignore[reportAny] -- the last event is TurnEnded


async def test_a_message_to_an_idle_agent_starts_its_turn(bus_env: BusEnv) -> None:
    await deliver(bus_env, sender="alpha", turn=NO_TURN, to="beta", text="Please check the data.")

    last = bus_env.fake.state.requests[-1]
    texts = json.dumps(last["messages"])
    assert "Message from alpha" in texts
    assert "Please check the data." in texts
    view = await bus_env.session.manager.get(bus_env.session.project_id, "beta")
    first = view.messages[0]
    assert (first.author_kind, first.author_name, first.from_agent) == ("user", "Alpha", "alpha")


async def test_a_reply_continues_the_exchange_at_the_next_hop(
    bus_env: BusEnv, databases: Databases
) -> None:
    project_id = bus_env.session.project_id
    beta_turn = await deliver(bus_env, sender="alpha", turn=NO_TURN, to="beta")
    await deliver(bus_env, sender="beta", turn=beta_turn, to="alpha", text="Done.")

    alpha = await bus_env.session.manager.get(project_id, "alpha")
    beta = await bus_env.session.manager.get(project_id, "beta")
    first = await message_row(databases, project_id, beta.messages[0].id)
    reply = await message_row(databases, project_id, alpha.messages[0].id)
    assert (first["hop"], reply["hop"]) == (1, 2)
    assert first["exchange_id"] is not None
    assert first["exchange_id"].startswith("xch_")
    assert reply["exchange_id"] == first["exchange_id"]


async def test_a_message_not_answering_one_starts_a_new_exchange(
    bus_env: BusEnv, databases: Databases
) -> None:
    project_id = bus_env.session.project_id
    await deliver(bus_env, sender="alpha", turn=NO_TURN, to="beta")
    await deliver(bus_env, sender="alpha", turn=NO_TURN, to="beta")

    beta = await bus_env.session.manager.get(project_id, "beta")
    first, second = [
        await message_row(databases, project_id, m.id) for m in beta.messages if m.from_agent
    ]
    assert first["hop"] == second["hop"] == 1
    assert first["exchange_id"] != second["exchange_id"]


async def test_the_seventh_hop_is_refused(bus_env: BusEnv) -> None:
    project_id = bus_env.session.project_id
    turn = NO_TURN
    sender, recipient = "alpha", "beta"
    for _ in range(6):
        turn = await deliver(bus_env, sender=sender, turn=turn, to=recipient)
        sender, recipient = recipient, sender

    with pytest.raises(MessageLimitReached, match="report to the user"):
        await bus_env.bus.send(
            project_id, sender=sender, sender_turn_id=turn, to=recipient, text="One more"
        )


async def test_a_token_budget_that_is_passed_refuses_the_reply(
    bus_env: BusEnv, databases: Databases
) -> None:
    project_id = bus_env.session.project_id
    beta_turn = await deliver(bus_env, sender="alpha", turn=NO_TURN, to="beta")
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            insert(llm_calls).values(
                id="llm_big",
                turn_id=beta_turn,
                agent="beta",
                model_address="x:y",
                prompt_tokens=900,
                completion_tokens=200,
                latency_ms=1,
                created_at="2026-10-24T00:00:00Z",
            )
        )
    await bus_env.settings.set(project_id, MessagingLimits(max_hops=6, max_tokens=1000))

    with pytest.raises(MessageLimitReached, match="report to the user"):
        await bus_env.bus.send(
            project_id, sender="beta", sender_turn_id=beta_turn, to="alpha", text="Reply"
        )


async def test_a_reply_within_the_token_budget_goes_through(bus_env: BusEnv) -> None:
    beta_turn = await deliver(bus_env, sender="alpha", turn=NO_TURN, to="beta")
    await bus_env.settings.set(
        bus_env.session.project_id, MessagingLimits(max_hops=6, max_tokens=10_000_000)
    )
    await deliver(bus_env, sender="beta", turn=beta_turn, to="alpha")


async def test_a_recipient_the_sender_may_not_message_is_refused(bus_env: BusEnv) -> None:
    for to, code in (
        ("gamma", "message_not_allowed"),
        ("alpha", "message_not_allowed"),
        ("nobody", "agent_not_found"),
    ):
        with pytest.raises(DomainError) as refused:
            await bus_env.bus.send(
                bus_env.session.project_id,
                sender="alpha",
                sender_turn_id=NO_TURN,
                to=to,
                text="Hi",
            )
        assert refused.value.code == code


async def test_sending_records_a_message_sent_event(bus_env: BusEnv) -> None:
    await deliver(bus_env, sender="alpha", turn=NO_TURN, to="beta")

    records = await bus_env.events.history(bus_env.session.project_id, types=["message.sent"])
    assert len(records) == 1
    record = records[0]
    assert record.source == "alpha"
    assert record.subject_id == str(record.payload["exchange_id"])
    assert record.payload["to"] == "beta"
    assert record.payload["hop"] == 1
    assert str(record.payload["exchange_id"]).startswith("xch_")


async def test_a_refused_post_leaves_no_exchange_behind(
    bus_env: BusEnv, databases: Databases
) -> None:
    project_id = bus_env.session.project_id

    with pytest.raises(DomainError) as refused:
        await bus_env.bus.send(
            project_id, sender="alpha", sender_turn_id=NO_TURN, to="beta", text="   "
        )

    assert refused.value.code == "message_empty"
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        assert (await conn.scalar(select(func.count()).select_from(exchanges))) == 0


async def test_the_bus_binds_and_takes_its_catalog_once(bus_env: BusEnv) -> None:
    with pytest.raises(RuntimeError, match="already bound"):
        bus_env.bus.bind(bus_env.session.manager)
    with pytest.raises(RuntimeError, match="already has its catalog"):
        bus_env.bus.use_catalog(AgentCatalog([], frozenset()))
