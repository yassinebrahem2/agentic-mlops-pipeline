from __future__ import annotations

import os
from pathlib import Path

import httpx2
import pytest

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.contract import StreamEvent, TurnEnded
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools import default_registry
from mlops_agents.config import RunMode, load_settings
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.workspace.service import WorkspaceService
from tests.conftest import MemoryKeyStore
from tests.integrations.test_github_auth import FakeGh

pytestmark = pytest.mark.live


async def test_live_turn_orchestrator_readme(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
) -> None:
    live_model = os.environ.get("MLOPS_AGENTS_LIVE_MODEL")
    if not live_model:
        pytest.skip("set MLOPS_AGENTS_LIVE_MODEL to run this live test")

    provider_name = live_model.split(":")[0] if ":" in live_model else "groq"
    key_env = f"{provider_name.upper()}_API_KEY"
    key_val = os.environ.get(key_env)
    if not key_val and provider_name in ("groq", "gemini", "anthropic", "openai"):
        pytest.skip(f"set {key_env} to run this live test")

    settings = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)})

    async with httpx2.AsyncClient(timeout=30.0) as http:
        github = GitHubAuth(databases, keystore, http, FakeGh(None))
        git = GitRunner(paths.bin, github.token)
        workspace_svc = WorkspaceService(settings, paths, databases, git, github, http)
        tools = default_registry()
        catalog = AgentCatalog.load(tools.names())
        providers_svc = ProvidersService(databases, keystore, http)
        settings_svc = AgentSettingsService(databases, providers_svc, catalog)

        manager = SessionManager(
            databases=databases,
            workspace=workspace_svc,
            catalog=catalog,
            tools=tools,
            settings=settings_svc,
            providers=providers_svc,
            http=http,
            git=git,
        )

        # Set up provider and model
        if provider_name == "groq":
            prov = await providers_svc.create_provider(
                ProviderCreate(
                    name="groq",
                    kind="hosted",
                    base_url="https://api.groq.com/openai/v1",
                )
            )
            if key_val:
                await providers_svc.add_key(prov.name, key_val)
        elif provider_name == "gemini":
            prov = await providers_svc.create_provider(
                ProviderCreate(
                    name="gemini",
                    kind="hosted",
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                )
            )
            if key_val:
                await providers_svc.add_key(prov.name, key_val)

        wire_id = live_model.split(":", 1)[1] if ":" in live_model else live_model
        await providers_svc.add_model(ModelCreate(provider=provider_name, wire_model_id=wire_id))
        await providers_svc.verify_model(live_model)

        repo_root = Path(__file__).parents[4]
        project = await workspace_svc.add_local(str(repo_root))
        await settings_svc.set_default_model("orchestrator", live_model)

        sub = manager.subscribe(project.id, "orchestrator", after_seq=None)
        await manager.send(project.id, "orchestrator", "What does this repository's README say?")

        events: list[StreamEvent] = []
        async for ev in sub:
            events.append(ev)
            if isinstance(ev, TurnEnded):
                break

        assert any(isinstance(e, TurnEnded) and e.data.status == "done" for e in events)

        view = await manager.get(project.id, "orchestrator")
        assert len(view.messages) >= 2
        agent_msg = view.messages[-1]
        text = "".join(p.text for p in agent_msg.parts if p.type == "text")
        print("\nLive model response:", text)
        assert len(text) > 0

        await manager.close()
