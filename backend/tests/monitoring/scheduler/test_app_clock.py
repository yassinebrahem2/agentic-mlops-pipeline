"""Tests for clock propagation through create_app and build_services."""

from __future__ import annotations

from pathlib import Path

from mlops_agents.api.app import create_app
from mlops_agents.config import RunMode, load_settings
from tests.api.conftest import NoGh
from tests.conftest import MemoryKeyStore
from tests.providers.fake_clock import FakeClock
from tests.sandbox.fakes import FakeDockerRunner


async def test_create_app_with_fake_clock(tmp_path: Path) -> None:
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
        services = app.state.services
        # Verify Services.clock is fake_clock
        assert services.clock is fake_clock
        # Verify AppScheduler has fake_clock
        assert services.scheduler._clock is fake_clock
