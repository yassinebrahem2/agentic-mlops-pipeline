"""A real session stack with a message bus and three agents: alpha and beta may message each
other, gamma is messaged by nobody."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx2

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.messaging.bus import SqliteMessageBus
from mlops_agents.agents.messaging.settings import MessagingSettings
from mlops_agents.agents.tools import default_registry
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import SessionEnv, build_session_env
from tests.conftest import STAGE_2_TOOLS, STAGE_3_HOOKS, MemoryKeyStore
from tests.fakes.fake_openai import RunningFake

DEFINITIONS = {
    "alpha": "can_message: [beta]",
    "beta": "can_message: [alpha]",
    "gamma": "can_message: []",
}


@dataclass(frozen=True)
class BusEnv:
    session: SessionEnv
    bus: SqliteMessageBus
    settings: MessagingSettings
    events: SqliteEventLog
    fake: RunningFake


async def build_bus_env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> BusEnv:
    defs = tmp_path / "defs"
    defs.mkdir()
    for name, line in DEFINITIONS.items():
        (defs / f"{name}.md").write_text(
            f"---\nname: {name}\ndisplay_name: {name.title()}\ndescription: {name}\n{line}\n"
            f"---\nYou are {name}.",
            encoding="utf-8",
        )
    events = SqliteEventLog(databases)
    settings = MessagingSettings(databases)
    bus = SqliteMessageBus(databases, events, settings)
    tools = default_registry(bus=bus)
    bus.use_catalog(
        AgentCatalog.load(
            tools.names() | STAGE_2_TOOLS, extra_dirs=[defs], hook_names=STAGE_3_HOOKS
        )
    )
    session = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake,
        model_http,
        git_env,
        extra_dirs=[defs],
        tools=tools,
        bus=bus,
    )
    for name in DEFINITIONS:
        await session.settings.set_default_model(name, f"{session.provider}:fake-tools")
    return BusEnv(session=session, bus=bus, settings=settings, events=events, fake=fake)
