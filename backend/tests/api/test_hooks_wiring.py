"""The hook registry as `build_services` wires it: the Orchestrator's turn gets the status board,
and the data_summary hook is registered."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.hooks.data_summary import DataSummaryHook
from mlops_agents.agents.hooks.names import DATA_SUMMARY_HOOK
from mlops_agents.api import services as services_module
from mlops_agents.api.services import Services
from mlops_agents.connections.contract import Connections
from mlops_agents.data.summaries import DataSummaries
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from tests.api.conftest import add_local_project, started_app
from tests.fakes.fake_openai import RunningFake


async def test_the_orchestrator_turn_gets_the_status_board_hook(
    app: FastAPI, fake_openai: RunningFake, tmp_path: Path, git_env: dict[str, str]
) -> None:
    services: Services = app.state.services
    provider = await services.providers.create_provider(
        ProviderCreate(name="fake", kind="self_hosted", base_url=fake_openai.base_url)
    )
    await services.providers.add_model(
        ModelCreate(provider=provider.name, wire_model_id="fake-tools")
    )
    await services.providers.verify_model("fake:fake-tools")
    await services.agent_settings.set_default_model("orchestrator", "fake:fake-tools")
    project_id = await add_local_project(app, tmp_path, git_env)
    fake_openai.state.requests.clear()

    await services.sessions.send(project_id, "orchestrator", "Who is busy?")
    async with asyncio.timeout(10):
        while True:
            view = await services.sessions.get(project_id, "orchestrator")
            if fake_openai.state.requests and not view.busy:
                break
            await asyncio.sleep(0.01)

    system = fake_openai.state.requests[0]["messages"][0]
    assert system["role"] == "system"
    content = str(system["content"])
    block = content[content.index('<data source="hook:status_board">') :]
    assert block.startswith('<data source="hook:status_board">\nStatus board:\n- Orchestrator: ')
    assert block.endswith("\n</data>")


async def test_the_data_summary_hook_is_registered_on_the_services_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registered before the catalog loads, over the one `DataSummaries` routes reach, so the
    hook and the table-detail route share one cache and its single flight."""
    built: list[_RecordingHook] = []
    loaded_with: list[frozenset[str]] = []
    real_load = AgentCatalog.load

    class _RecordingHook(DataSummaryHook):
        def __init__(self, connections: Connections, summaries: DataSummaries) -> None:
            super().__init__(connections, summaries)
            self.summaries = summaries
            built.append(self)

    def recording_load(*args: Any, **kwargs: Any) -> AgentCatalog:
        loaded_with.append(kwargs["hook_names"])
        return real_load(*args, **kwargs)

    monkeypatch.setattr(services_module, "DataSummaryHook", _RecordingHook)
    monkeypatch.setattr(AgentCatalog, "load", staticmethod(recording_load))

    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        (hook,) = built
        assert hook.summaries is services.data_summaries
        assert hook.name == DATA_SUMMARY_HOOK
        assert DATA_SUMMARY_HOOK in loaded_with[0]
