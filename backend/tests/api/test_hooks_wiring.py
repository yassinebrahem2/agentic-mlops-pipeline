"""The hook registry as `build_services` wires it: the Orchestrator's turn gets the status board."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI

from mlops_agents.api.services import Services
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from tests.api.conftest import add_local_project
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
