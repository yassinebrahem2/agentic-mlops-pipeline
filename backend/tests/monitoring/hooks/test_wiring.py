"""Tests for HooksService and HandoffHooks wiring in build_services."""

from __future__ import annotations

from pathlib import Path

import pytest

from mlops_agents.agents.tools.hold import HoldTool
from mlops_agents.agents.tools.run_hook import RunHookTool
from mlops_agents.api.app import create_app
from mlops_agents.api.services import Services
from mlops_agents.config import RunMode, load_settings
from mlops_agents.ids import new_id
from mlops_agents.monitoring.hooks.kind import HooksKind
from mlops_agents.monitoring.hooks.query import HooksQueryProvider
from mlops_agents.monitoring.hooks.service import HooksService
from mlops_agents.proposals.payloads import HandoffPayload
from tests.api.conftest import NoGh
from tests.conftest import MemoryKeyStore
from tests.proposals.samples import command
from tests.proposals.samples import handoff as sample_handoff
from tests.providers.fake_clock import FakeClock
from tests.sandbox.fakes import FakeDockerRunner


@pytest.mark.asyncio
async def test_build_services_hooks_wiring(
    tmp_path: Path,
) -> None:
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

        # Verify hooks exists on services
        assert isinstance(services.hooks, HooksService)

        # Verify RunHook and Hold in services.tools
        assert isinstance(services.tools.get("RunHook"), RunHookTool)
        assert isinstance(services.tools.get("Hold"), HoldTool)

        # Verify hooks query provider is registered
        assert isinstance(services.query_providers.get("hooks"), HooksQueryProvider)

        # Verify hooks proposal kind is registered
        assert isinstance(services.proposal_kinds.get("hooks"), HooksKind)

        # Create a project directly
        project_id = new_id("prj")
        # Ensure project DB exists
        _ = await services.databases.project(project_id)

        cmd = command(kind="rollback", template="echo rb")
        payload = HandoffPayload.model_validate(sample_handoff(commands=[cmd], models=[]))

        # Storing via handoffs triggers the observer
        await services.handoffs.store(project_id, "prp_wire", payload)

        hooks = await services.hooks.list(project_id)
        assert len(hooks) == 1
        assert hooks[0].name == "rollback"
        assert hooks[0].source == "handoff"
