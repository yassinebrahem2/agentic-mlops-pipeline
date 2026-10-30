"""The kickoff: one "Handoff" message to each audit agent, exactly once, nothing lost."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

import httpx2
import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from mlops_agents.agents.runtime.contract import MessageView
from mlops_agents.clock import now_iso
from mlops_agents.onboarding.briefs import (
    AUDIT_BRIEFS,
    KICKOFF_AGENTS,
)
from mlops_agents.onboarding.handoff_preview import sections
from mlops_agents.onboarding.kickoff import HANDOFF_AUTHOR, Kickoff, kickoff_text
from mlops_agents.paths import DataPaths
from mlops_agents.proposals.handoffs import SqliteHandoffs
from mlops_agents.proposals.payloads import HandoffPayload
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import (
    exchanges,
    kickoff,
    messages,
    projects,
    sessions,
)
from tests.agents.runtime.session_env import SessionEnv, until_idle
from tests.conftest import MemoryKeyStore
from tests.onboarding.kickoff_env import (
    PROPOSAL,
    all_idle,
    handoff_messages,
    kickoff_rows,
    marker,
    restart,
    sample_payload,
)


@pytest.fixture
def handoffs(databases: Databases) -> SqliteHandoffs:
    return SqliteHandoffs(databases)


async def _stored(
    handoffs: SqliteHandoffs, project_id: str, proposal_id: str = PROPOSAL
) -> tuple[int, HandoffPayload]:
    payload = sample_payload()
    return await handoffs.store(project_id, proposal_id, payload), payload


# Posting


async def test_start_posts_one_handoff_message_to_each_audit_agent(
    env: SessionEnv, databases: Databases, handoffs: SqliteHandoffs
) -> None:
    version, payload = await _stored(handoffs, env.project_id)
    kick = Kickoff(databases, env.manager, handoffs)

    posted = await kick.start(env.project_id, version, PROPOSAL, payload)
    await all_idle(env.manager, env.project_id)

    assert posted == list(KICKOFF_AGENTS)
    found = await handoff_messages(databases, env.project_id)
    assert sorted(m.session_agent for m in found) == sorted(KICKOFF_AGENTS)
    rows = await kickoff_rows(databases, env.project_id)
    for message in found:
        agent = message.session_agent
        assert message.author_name == HANDOFF_AUTHOR == "Handoff"
        assert message.from_agent == agent
        assert message.exchange_id is None
        assert message.hop is None
        assert message.text == kickoff_text(agent, PROPOSAL, payload)
        assert message.text.startswith(marker(PROPOSAL))
        assert all(section in message.text for section in sections(payload).values())
        assert message.text.endswith(AUDIT_BRIEFS[agent])
        assert rows[(version, agent)]["status"] == "posted"
        assert rows[(version, agent)]["message_id"] == message.id
        assert rows[(version, agent)]["posted_at"] is not None
    assert len(rows) == len(KICKOFF_AGENTS)


async def test_a_second_start_for_the_same_proposal_posts_nothing(
    env: SessionEnv, databases: Databases, handoffs: SqliteHandoffs
) -> None:
    version, payload = await _stored(handoffs, env.project_id)
    kick = Kickoff(databases, env.manager, handoffs)

    await kick.start(env.project_id, version, PROPOSAL, payload)
    again = await kick.start(env.project_id, version, PROPOSAL, payload)
    await all_idle(env.manager, env.project_id)

    assert again == []
    assert len(await handoff_messages(databases, env.project_id)) == len(KICKOFF_AGENTS)


async def test_an_agent_without_a_model_keeps_its_posted_row_and_is_not_posted_again(
    env: SessionEnv, databases: Databases, handoffs: SqliteHandoffs
) -> None:
    # No kickoff agent has a model in this environment: each turn is refused.
    version, payload = await _stored(handoffs, env.project_id)
    kick = Kickoff(databases, env.manager, handoffs)

    await kick.start(env.project_id, version, PROPOSAL, payload)
    await all_idle(env.manager, env.project_id)
    await kick.resume()
    await all_idle(env.manager, env.project_id)

    view = await env.manager.get(env.project_id, "retraining")
    assert view.messages[0].author_name == HANDOFF_AUTHOR
    assert "Choose a model for Retraining" in view.model_dump_json()  # the stage 1 refusal
    rows = await kickoff_rows(databases, env.project_id)
    assert {r["status"] for r in rows.values()} == {"posted"}
    assert len(await handoff_messages(databases, env.project_id)) == len(KICKOFF_AGENTS)


# Crash windows


async def test_resume_marks_a_post_whose_mark_was_lost_and_posts_nothing(
    env: SessionEnv, databases: Databases, handoffs: SqliteHandoffs
) -> None:
    version, payload = await _stored(handoffs, env.project_id)
    kick = Kickoff(databases, env.manager, handoffs)
    await kick.start(env.project_id, version, PROPOSAL, payload)
    await all_idle(env.manager, env.project_id)
    before = await kickoff_rows(databases, env.project_id)
    engine = await databases.project(env.project_id)
    async with engine.begin() as conn:  # the crash: posted, never marked
        await conn.execute(
            update(kickoff)
            .where(kickoff.c.agent == "retraining")
            .values(status="pending", message_id=None, posted_at=None)
        )

    await Kickoff(databases, env.manager, handoffs).resume()
    await all_idle(env.manager, env.project_id)

    after = await kickoff_rows(databases, env.project_id)
    assert after[(version, "retraining")]["status"] == "posted"
    assert (
        after[(version, "retraining")]["message_id"]
        == before[(version, "retraining")]["message_id"]
    )
    assert len(await handoff_messages(databases, env.project_id)) == len(KICKOFF_AGENTS)


async def test_resume_finishes_a_kickoff_lost_before_any_row_was_written(
    env: SessionEnv, databases: Databases, handoffs: SqliteHandoffs
) -> None:
    version, _ = await _stored(handoffs, env.project_id)  # the crash: stored, nothing else

    await Kickoff(databases, env.manager, handoffs).resume()
    await all_idle(env.manager, env.project_id)

    rows = await kickoff_rows(databases, env.project_id)
    assert {agent for (_, agent) in rows} == set(KICKOFF_AGENTS)
    assert {r["status"] for r in rows.values()} == {"posted"}
    assert {v for (v, _) in rows} == {version}
    found = await handoff_messages(databases, env.project_id)
    assert sorted(m.session_agent for m in found) == sorted(KICKOFF_AGENTS)


async def test_a_failing_post_leaves_its_row_pending_and_the_next_resume_posts_only_it(
    env: SessionEnv,
    databases: Databases,
    handoffs: SqliteHandoffs,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    version, payload = await _stored(handoffs, env.project_id)
    real_post = env.manager.post

    async def post_failing_for_retraining(project_id: str, agent: str, **kw: Any) -> MessageView:
        if agent == "retraining":
            raise RuntimeError("the session store is unavailable")
        return await real_post(project_id, agent, **kw)

    monkeypatch.setattr(env.manager, "post", post_failing_for_retraining)
    kick = Kickoff(databases, env.manager, handoffs)

    with caplog.at_level(logging.ERROR, logger="mlops_agents.onboarding.kickoff"):
        posted = await kick.start(env.project_id, version, PROPOSAL, payload)
    await all_idle(env.manager, env.project_id)

    assert posted == [a for a in KICKOFF_AGENTS if a != "retraining"]
    assert any(r.exc_info is not None and "retraining" in r.getMessage() for r in caplog.records)
    rows = await kickoff_rows(databases, env.project_id)
    assert rows[(version, "retraining")]["status"] == "pending"
    assert rows[(version, "retraining")]["message_id"] is None
    assert len(await handoff_messages(databases, env.project_id)) == len(KICKOFF_AGENTS) - 1

    monkeypatch.setattr(env.manager, "post", real_post)
    await kick.resume()
    await all_idle(env.manager, env.project_id)

    rows = await kickoff_rows(databases, env.project_id)
    assert {r["status"] for r in rows.values()} == {"posted"}
    found = await handoff_messages(databases, env.project_id)
    assert sorted(m.session_agent for m in found) == sorted(KICKOFF_AGENTS)


async def test_a_failing_mark_counts_as_posted_and_a_repeat_posts_nothing_twice(
    env: SessionEnv,
    databases: Databases,
    handoffs: SqliteHandoffs,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    version, payload = await _stored(handoffs, env.project_id)
    kick = Kickoff(databases, env.manager, handoffs)
    real_mark = kick._mark  # pyright: ignore[reportPrivateUsage]

    async def mark_failing_for_retraining(
        engine: AsyncEngine, version: int, agent: str, message_id: str
    ) -> None:
        if agent == "retraining":
            raise OperationalError("UPDATE kickoff", {}, sqlite3.OperationalError("disk I/O error"))
        await real_mark(engine, version, agent, message_id)

    monkeypatch.setattr(kick, "_mark", mark_failing_for_retraining)

    with caplog.at_level(logging.ERROR, logger="mlops_agents.onboarding.kickoff"):
        posted = await kick.start(env.project_id, version, PROPOSAL, payload)
    await all_idle(env.manager, env.project_id)

    # The post went through, so it counts; only its mark is left for the marker guard.
    assert posted == list(KICKOFF_AGENTS)
    assert any(r.exc_info is not None and "retraining" in r.getMessage() for r in caplog.records)
    rows = await kickoff_rows(databases, env.project_id)
    assert rows[(version, "retraining")]["status"] == "pending"
    assert {rows[(version, a)]["status"] for a in KICKOFF_AGENTS if a != "retraining"} == {"posted"}
    assert len(await handoff_messages(databases, env.project_id)) == len(KICKOFF_AGENTS)

    monkeypatch.setattr(kick, "_mark", real_mark)
    again = await kick.start(env.project_id, version, PROPOSAL, payload)
    await kick.resume()
    await all_idle(env.manager, env.project_id)

    assert again == []
    rows = await kickoff_rows(databases, env.project_id)
    assert {r["status"] for r in rows.values()} == {"posted"}
    found = await handoff_messages(databases, env.project_id)
    assert sorted(m.session_agent for m in found) == sorted(KICKOFF_AGENTS)
    retraining = next(m for m in found if m.session_agent == "retraining")
    assert rows[(version, "retraining")]["message_id"] == retraining.id


# Restart


async def test_a_restart_neither_repeats_nor_loses_a_kickoff(
    env: SessionEnv,
    databases: Databases,
    handoffs: SqliteHandoffs,
    paths: DataPaths,
    keystore: MemoryKeyStore,
    model_http: httpx2.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_version, payload = await _stored(handoffs, env.project_id)
    await Kickoff(databases, env.manager, handoffs).start(
        env.project_id, first_version, PROPOSAL, payload
    )
    # A second handoff whose every post fails before the process stops: it must not be lost.
    second_version, second_payload = await _stored(handoffs, env.project_id, "prp_second")

    async def post_down(*args: Any, **kwargs: Any) -> MessageView:
        raise RuntimeError("stopping")

    monkeypatch.setattr(env.manager, "post", post_down)
    lost = await Kickoff(databases, env.manager, handoffs).start(
        env.project_id, second_version, "prp_second", second_payload
    )
    assert lost == []
    await all_idle(env.manager, env.project_id)
    await env.manager.close()
    await databases.dispose()

    app = await restart(paths, keystore, model_http)
    try:
        await Kickoff(app.databases, app.manager, SqliteHandoffs(app.databases)).resume()
        await all_idle(app.manager, env.project_id)
        found = await handoff_messages(app.databases, env.project_id)
        rows = await kickoff_rows(app.databases, env.project_id)
    finally:
        await app.close()

    first = [m for m in found if marker(PROPOSAL) in m.text]
    second = [m for m in found if marker("prp_second") in m.text]
    assert sorted(m.session_agent for m in first) == sorted(KICKOFF_AGENTS)
    assert sorted(m.session_agent for m in second) == sorted(KICKOFF_AGENTS)
    assert {r["status"] for r in rows.values()} == {"posted"}
    assert len(rows) == 2 * len(KICKOFF_AGENTS)


async def test_resume_skips_a_project_whose_database_cannot_be_opened(
    env: SessionEnv,
    databases: Databases,
    handoffs: SqliteHandoffs,
    paths: DataPaths,
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = "prj_broken"
    app_engine = await databases.app()
    async with app_engine.begin() as conn:
        await conn.execute(
            insert(projects).values(
                id=broken,
                name="broken",
                source="local",
                local_path=str(paths.root / "broken"),
                local_path_key=str(paths.root / "broken").lower(),
                status="ok",
                created_at=now_iso(),
            )
        )
    paths.project_db(broken).parent.mkdir(parents=True, exist_ok=True)
    paths.project_db(broken).write_bytes(b"this is not a database " * 200)
    await _stored(handoffs, env.project_id)

    with caplog.at_level(logging.WARNING, logger="mlops_agents.onboarding.kickoff"):
        await Kickoff(databases, env.manager, handoffs).resume()
    await all_idle(env.manager, env.project_id)

    warned = [r for r in caplog.records if broken in r.getMessage()]
    assert warned and warned[0].levelno == logging.WARNING and warned[0].exc_info is not None
    assert len(await handoff_messages(databases, env.project_id)) == len(KICKOFF_AGENTS)


# Newer handoffs


async def test_a_newer_handoff_starts_its_own_kickoff_and_a_reapplied_one_posts_none(
    env: SessionEnv, databases: Databases, handoffs: SqliteHandoffs
) -> None:
    kick = Kickoff(databases, env.manager, handoffs)
    first_version, payload = await _stored(handoffs, env.project_id)
    await kick.start(env.project_id, first_version, PROPOSAL, payload)
    second_version = await handoffs.store(env.project_id, "prp_second", payload)
    assert second_version != first_version

    newer = await kick.start(env.project_id, second_version, "prp_second", payload)
    again = await kick.start(env.project_id, second_version, "prp_second", payload)
    older_again = await kick.start(env.project_id, first_version, PROPOSAL, payload)
    await all_idle(env.manager, env.project_id)

    assert newer == list(KICKOFF_AGENTS)
    assert again == []
    assert older_again == []
    found = await handoff_messages(databases, env.project_id)
    assert len(found) == 2 * len(KICKOFF_AGENTS)
    second = [m for m in found if marker("prp_second") in m.text]
    assert sorted(m.session_agent for m in second) == sorted(KICKOFF_AGENTS)


# Onboarding stays out of it


async def test_replies_stay_in_each_agents_session_and_onboarding_is_not_woken(
    env: SessionEnv, databases: Databases, handoffs: SqliteHandoffs
) -> None:
    for agent in KICKOFF_AGENTS:
        await env.settings.set_default_model(agent, f"{env.provider}:fake-tools")
    version, payload = await _stored(handoffs, env.project_id)

    await Kickoff(databases, env.manager, handoffs).start(
        env.project_id, version, PROPOSAL, payload
    )
    await all_idle(env.manager, env.project_id)
    await until_idle(env.manager, env.project_id, "onboarding")

    engine = await databases.project(env.project_id)
    async with engine.connect() as conn:
        exchange_count = await conn.scalar(select(func.count()).select_from(exchanges))
        with_exchange = await conn.scalar(
            select(func.count()).select_from(messages).where(messages.c.exchange_id.is_not(None))
        )
        onboarding_messages = await conn.scalar(
            select(func.count())
            .select_from(messages.join(sessions, sessions.c.id == messages.c.session_id))
            .where(sessions.c.agent == "onboarding")
        )
    assert exchange_count == 0
    assert with_exchange == 0
    assert onboarding_messages == 0
    for agent in KICKOFF_AGENTS:
        view = await env.manager.get(env.project_id, agent)
        assert any(m.author_kind == "agent" for m in view.messages), agent
