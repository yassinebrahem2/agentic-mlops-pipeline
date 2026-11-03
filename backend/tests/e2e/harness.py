"""End-to-end test harness for stages 4 to 6.

Provides:
- SettableClock: Shared fake clock (Clock protocol) with settable instant/now, no sleep.
- started_app / restarted_app with shared data folder, keystore, FakeDockerRunner,
  fake OpenAI, fake GitHub, and fake Discord servers.
- run_due(services, now): drives Scheduler.run_due(project_id, now) at an instant.
- wait_for_agent_idle: polls an agent's session until busy == False.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

import httpx2
from fastapi import FastAPI

import mlops_agents.integrations.github.api as gh_api
from mlops_agents.api.services import Services
from mlops_agents.config import RunMode
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from tests.api.conftest import LOCAL_URL, signed_in, started_app
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_discord import FakeDiscordState, RunningFakeDiscord, start_fake_discord
from tests.fakes.fake_github import FakeGitHubState, RunningFakeGitHub, start_fake_github
from tests.fakes.fake_openai import FakeState, RunningFake, start_fake_server
from tests.sandbox.fakes import FakeDockerRunner

DEFAULT_START: Final = datetime(2026, 10, 31, 0, 0, 0, tzinfo=UTC)

DEFAULT_AGENT_MODELS: Final[dict[str, str]] = {
    "alert-prioritization": "fake-alert-prioritization",
    "data-quality": "fake-data-quality",
    "deployment": "fake-deployment-rollback",
    "onboarding": "fake-onboarding-night",
}


class SettableClock:
    """A Clock protocol implementation whose instant is controlled by tests; sleep does nothing."""

    def __init__(self, start: datetime = DEFAULT_START) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def set(self, now: datetime) -> None:
        self.current = now

    def advance(self, seconds: float | timedelta) -> None:
        if isinstance(seconds, timedelta):
            self.current += seconds
        else:
            self.current += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0)


async def run_due(
    services: Services,
    arg1: datetime | str,
    arg2: datetime | str | None = None,
) -> None:
    """Evaluates dues and executes unfinished jobs for project_id at an instant.

    Accepts (services, now, project_id=None) or (services, project_id, now).
    When project_id is None, runs for all projects in workspace.
    """
    now: datetime
    project_id: str | None
    if isinstance(arg1, datetime):
        now = arg1
        project_id = cast(str | None, arg2)
    elif isinstance(arg2, datetime):
        now = arg2
        project_id = str(arg1)
    else:
        raise ValueError("run_due requires a datetime for the 'now' parameter")

    if project_id is not None:
        await services.scheduler.run_due(project_id, now)
    else:
        projects = await services.workspace.list_projects()
        for p in projects:
            await services.scheduler.run_due(p.id, now)


async def wait_for_agent_idle(
    http: httpx2.AsyncClient,
    project_id: str,
    agent: str,
    *,
    timeout_s: float = 30.0,
    poll_interval: float = 0.05,
) -> dict[str, Any]:
    """Polls /api/projects/{id}/agents/{agent}/session until busy == False."""
    async with asyncio.timeout(timeout_s):
        while True:
            response = await http.get(f"/api/projects/{project_id}/agents/{agent}/session")
            assert response.status_code == 200, response.text
            session = cast(dict[str, Any], response.json())
            if not session.get("busy", False):
                return session
            await asyncio.sleep(poll_interval)


@dataclass
class E2EHarness:
    """Shared environment across app restarts: data directory, keystore, clock, and fakes."""

    home: Path
    keystore: MemoryKeyStore
    clock: SettableClock
    docker_runner: FakeDockerRunner
    fake_openai: RunningFake
    fake_github: RunningFakeGitHub
    fake_discord: RunningFakeDiscord

    @asynccontextmanager
    async def started_app(
        self,
        mode: RunMode = RunMode.LOCAL,
        settings_env: Mapping[str, str] | None = None,
    ) -> AsyncGenerator[FastAPI]:
        """Starts an in-process FastAPI app using the shared data folder, clock, and keystore."""
        async with started_app(
            self.home,
            mode=mode,
            settings_env=settings_env,
            docker_runner=self.docker_runner,
            clock=self.clock,
            keystore=self.keystore,
        ) as app:
            yield app

    def client(
        self,
        app: FastAPI,
        base_url: str = LOCAL_URL,
        *,
        in_process: bool = True,
        timeout: httpx2.Timeout | float | None = None,
    ) -> httpx2.AsyncClient:
        """Returns a signed-in client for the started application."""
        return signed_in(app, base_url, in_process=in_process, timeout=timeout)

    async def setup_models(
        self,
        services: Services,
        model_mappings: Mapping[str, str] | None = None,
    ) -> None:
        """Registers the fake OpenAI provider and assigns scripted models to agents."""
        mappings = model_mappings or DEFAULT_AGENT_MODELS
        provider = await services.providers.create_provider(
            ProviderCreate(name="fake", kind="self_hosted", base_url=self.fake_openai.base_url)
        )
        wire_ids = sorted(set(mappings.values()))
        for wire_id in wire_ids:
            await services.providers.add_model(
                ModelCreate(provider=provider.name, wire_model_id=wire_id)
            )
            await services.providers.verify_model(f"fake:{wire_id}")
        for agent, wire_id in mappings.items():
            await services.agent_settings.set_default_model(agent, f"fake:{wire_id}")


@asynccontextmanager
async def create_e2e_harness(
    home: Path,
    start_time: datetime = DEFAULT_START,
    *,
    keystore: MemoryKeyStore | None = None,
    docker_runner: FakeDockerRunner | None = None,
    openai_state: FakeState | None = None,
    github_state: FakeGitHubState | None = None,
    discord_state: FakeDiscordState | None = None,
) -> AsyncGenerator[E2EHarness]:
    """Sets up the E2EHarness with fake servers and patches GitHub API to point to fake GitHub."""
    clock = SettableClock(start=start_time)
    store = keystore if keystore is not None else MemoryKeyStore()
    runner = docker_runner if docker_runner is not None else FakeDockerRunner()

    running_openai = start_fake_server(openai_state or FakeState())
    running_github = start_fake_github(github_state or FakeGitHubState())
    running_discord = start_fake_discord(discord_state or FakeDiscordState())

    original_github_api = gh_api.GITHUB_API
    gh_api.GITHUB_API = running_github.base_url

    harness = E2EHarness(
        home=home,
        keystore=store,
        clock=clock,
        docker_runner=runner,
        fake_openai=running_openai,
        fake_github=running_github,
        fake_discord=running_discord,
    )
    try:
        yield harness
    finally:
        gh_api.GITHUB_API = original_github_api
        running_discord.stop()
        running_github.stop()
        running_openai.stop()
