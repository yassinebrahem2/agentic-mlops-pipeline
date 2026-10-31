"""Tests for SqliteRules and RuleKind wiring in build_services."""

from __future__ import annotations

from pathlib import Path

import pytest

from mlops_agents.api.app import create_app
from mlops_agents.api.services import Services
from mlops_agents.config import RunMode, load_settings
from mlops_agents.monitoring.rules.kind import RuleKind
from mlops_agents.monitoring.rules.store import SqliteRules
from tests.api.conftest import NoGh
from tests.conftest import MemoryKeyStore
from tests.providers.fake_clock import FakeClock
from tests.sandbox.fakes import FakeDockerRunner


@pytest.mark.asyncio
async def test_build_services_rules_wiring(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = {"MLOPS_AGENTS_HOME": str(home)}
    settings = load_settings(RunMode.LOCAL, port=8765, env=env)

    fake_clock = FakeClock()
    app = create_app(
        settings,
        keystore=MemoryKeyStore(),
        gh=NoGh(),
        docker_runner=FakeDockerRunner(),
        clock=fake_clock,
    )

    async with app.router.lifespan_context(app):
        services: Services = app.state.services

        # Verify rules exists on services
        assert isinstance(services.rules, SqliteRules)

        # Verify rule proposal kind is registered
        assert isinstance(services.proposal_kinds.get("rule"), RuleKind)
