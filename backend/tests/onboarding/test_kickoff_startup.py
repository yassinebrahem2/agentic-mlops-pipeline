"""The app finishes lost kickoffs at startup, before it serves, and one project's unreadable
data never stops it from starting."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import update

from mlops_agents.api.services import Services
from mlops_agents.onboarding.briefs import KICKOFF_AGENTS
from mlops_agents.onboarding.kickoff import Kickoff
from mlops_agents.storage.tables import handoff_versions
from tests.api.conftest import add_local_project, started_app
from tests.onboarding.kickoff_env import (
    PROPOSAL,
    all_idle,
    handoff_messages,
    kickoff_rows,
    sample_payload,
)


async def test_the_app_finishes_a_lost_kickoff_before_it_serves(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        assert isinstance(services.kickoff, Kickoff)
        project_id = await add_local_project(app, tmp_path, git_env)
        await services.handoffs.store(project_id, PROPOSAL, sample_payload())  # then the crash

    async with started_app(tmp_path / "data") as app:
        services = app.state.services
        rows = await kickoff_rows(services.databases, project_id)
        found = await handoff_messages(services.databases, project_id)
        await all_idle(services.sessions, project_id)

    assert {agent for (_, agent) in rows} == set(KICKOFF_AGENTS)
    assert {r["status"] for r in rows.values()} == {"posted"}
    assert sorted(m.session_agent for m in found) == sorted(KICKOFF_AGENTS)


async def test_an_unreadable_stored_handoff_is_skipped_and_the_app_still_starts(
    tmp_path: Path, git_env: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        bad = await add_local_project(app, tmp_path / "bad", git_env)
        good = await add_local_project(app, tmp_path / "good", git_env)
        for project_id in (bad, good):  # stored, then the crash before any kickoff
            await services.handoffs.store(project_id, PROPOSAL, sample_payload())
        engine = await services.databases.project(bad)
        async with engine.begin() as conn:  # no longer valid for today's payload model
            await conn.execute(update(handoff_versions).values(payload='{"models": "none"}'))

    with caplog.at_level(logging.WARNING, logger="mlops_agents.onboarding.kickoff"):
        async with started_app(tmp_path / "data") as app:
            services = app.state.services
            good_rows = await kickoff_rows(services.databases, good)
            bad_rows = await kickoff_rows(services.databases, bad)
            found = await handoff_messages(services.databases, good)
            await all_idle(services.sessions, good)

    assert {r["status"] for r in good_rows.values()} == {"posted"}
    assert sorted(m.session_agent for m in found) == sorted(KICKOFF_AGENTS)
    assert bad_rows == {}
    warned = [r for r in caplog.records if bad in r.getMessage()]
    assert warned and warned[0].levelno == logging.WARNING
    assert warned[0].exc_info is not None and warned[0].exc_info[0] is ValidationError
