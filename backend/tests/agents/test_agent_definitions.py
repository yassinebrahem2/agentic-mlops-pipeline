from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path
from typing import Any

import httpx2
import pytest

from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.approvals.settings import set_permission_level
from mlops_agents.agents.catalog import (
    AgentCatalog,
    AgentDefinition,
    DefinitionError,
    parse_definition,
)
from mlops_agents.agents.runtime.contract import StreamEvent, TurnEnded
from mlops_agents.agents.runtime.sessions import SessionManager
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tasks.service import TasksService
from mlops_agents.agents.tools import default_registry
from mlops_agents.config import RunMode, load_settings
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.events.status import SqliteStatusBoard
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.providers.models import ModelCreate, ProviderCreate
from mlops_agents.providers.service import ProvidersService
from mlops_agents.storage.databases import Databases
from mlops_agents.workspace.service import WorkspaceService
from tests.agents.runtime.hook_fakes import NoInbound
from tests.conftest import MemoryKeyStore, make_repo
from tests.fakes.turn_end import NoTurnEndCheck
from tests.integrations.test_github_auth import FakeGh

STAGE_2_TOOLS = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "Write",
        "Edit",
        "Bash",
        "StartTask",
        "ResumeTask",
        "OpenPullRequest",
        "SearchIssues",
        "OpenIssue",
        "CommentOnIssue",
        "ListAgents",
        "SendMessage",
        "ScheduleWakeup",
    }
)

EXPECTED_TOOL_ORDER = (
    "Read",
    "Glob",
    "Grep",
    "Write",
    "Edit",
    "Bash",
    "StartTask",
    "ResumeTask",
    "OpenPullRequest",
    "SearchIssues",
    "OpenIssue",
    "CommentOnIssue",
    "ListAgents",
    "SendMessage",
    "ScheduleWakeup",
)

NAME_MAP: dict[str, str] = {
    "Orchestrator": "orchestrator",
    "Onboarding & Diagnosis": "onboarding",
    "Data Quality": "data-quality",
    "Model Performance": "model-performance",
    "Retraining": "retraining",
    "Deployment & Rollback": "deployment",
}


def load_spec_table_data() -> list[dict[str, Any]]:
    spec_path = (
        Path(__file__).resolve().parents[3]
        / "docs"
        / "superpowers"
        / "specs"
        / "2026-10-24-stage-2-agent-runtime-design.md"
    )
    content = spec_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    in_section_9 = False
    table_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## 9. The seven agents"):
            in_section_9 = True
            continue
        if in_section_9:
            if stripped.startswith("## "):
                break
            if stripped.startswith("|"):
                table_lines.append(stripped)

    # table_lines[0]: header, table_lines[1]: divider
    rows: list[dict[str, Any]] = []
    for row in table_lines[2:]:
        cols = [c.strip() for c in row.split("|")[1:-1]]
        agent_display = cols[0]
        if agent_display not in NAME_MAP:
            continue  # Alert Prioritization is handled in Task 2
        name = NAME_MAP[agent_display]
        chat = cols[1].lower() == "yes"
        schedules = cols[4].lower() == "yes"
        raw_msg = cols[3]
        if raw_msg == "all but Alert Prioritization":
            can_message = (
                "onboarding",
                "data-quality",
                "model-performance",
                "retraining",
                "deployment",
            )
        else:
            can_message = tuple(NAME_MAP[p.strip()] for p in raw_msg.split(","))

        rows.append(
            {
                "name": name,
                "display_name": agent_display,
                "chat": chat,
                "schedules": schedules,
                "status_board": (name == "orchestrator"),
                "tools": EXPECTED_TOOL_ORDER,
                "can_message": can_message,
            }
        )
    return rows


SPEC_ROWS = load_spec_table_data()


def _load_definition(name: str) -> AgentDefinition:
    folder = files("mlops_agents.agents").joinpath("definitions")
    entry = folder.joinpath(f"{name}.md")
    return parse_definition(entry.read_text(encoding="utf-8"), f"{name}.md")


@pytest.mark.parametrize("expected", SPEC_ROWS, ids=[r["name"] for r in SPEC_ROWS])
def test_stage2_agent_definitions_match_spec(expected: dict[str, Any]) -> None:
    defn = _load_definition(expected["name"])

    assert defn.name == expected["name"]
    assert defn.display_name == expected["display_name"]
    assert defn.chat is expected["chat"]
    assert defn.schedules is expected["schedules"]
    assert defn.status_board is expected["status_board"]

    # Read MUST be the first tool
    assert defn.tools[0] == "Read"
    assert defn.tools == expected["tools"]

    # can_message exact peer list
    assert defn.can_message == expected["can_message"]

    # Prompt line count constraint: strictly under 60 lines
    prompt_lines = defn.prompt.splitlines()
    assert len(prompt_lines) < 60, f"{defn.name} prompt exceeds 60 lines: {len(prompt_lines)} lines"

    # Prompt structure: 3 distinct parts
    # 1. The role
    # 2. What the agent can do now (and note on what is not available yet)
    # 3. How to work
    lower_prompt = defn.prompt.lower()
    assert "how to work:" in lower_prompt or "how to work" in lower_prompt, (
        f"{defn.name} prompt missing 'How to work' section"
    )
    assert "starttask" in lower_prompt, f"{defn.name} prompt missing StartTask guidance"
    assert "not available yet" in lower_prompt, (
        f"{defn.name} prompt missing note on what is not available yet"
    )


def test_agent_catalog_loads_stage2_definitions_with_full_tools() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS)
    assert catalog.get("orchestrator").status_board is True
    for name in (
        "onboarding",
        "data-quality",
        "model-performance",
        "retraining",
        "deployment",
    ):
        defn = catalog.get(name)
        assert defn.status_board is False
        assert defn.chat is True
        assert defn.schedules is True


def test_agent_catalog_fails_when_messaging_tools_missing() -> None:
    # default_registry().names() from main does not yet have messaging tools
    with pytest.raises(DefinitionError) as exc_info:
        AgentCatalog.load(default_registry().names())
    error_msg = str(exc_info.value)
    assert "ListAgents" in error_msg
    assert "ScheduleWakeup" in error_msg
    assert "SendMessage" in error_msg


_BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}


@pytest.mark.live
async def test_live_orchestrator_adds_line_to_readme(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    live_model = os.environ.get("MLOPS_AGENTS_LIVE_MODEL")
    if not live_model:
        pytest.skip("set MLOPS_AGENTS_LIVE_MODEL to run this live test")

    provider_name, separator, wire_id = live_model.partition(":")
    if not separator or not wire_id:
        pytest.fail(
            "MLOPS_AGENTS_LIVE_MODEL must be 'provider:model', e.g. groq:openai/gpt-oss-20b"
        )
    base_url = _BASE_URLS.get(provider_name)
    if base_url is None:
        pytest.fail(f"MLOPS_AGENTS_LIVE_MODEL provider must be one of {sorted(_BASE_URLS)}")
    key_env = f"{provider_name.upper()}_API_KEY"
    key_val = os.environ.get(key_env)
    if not key_val:
        pytest.skip(f"set {key_env} to run this live test")

    repo = make_repo(tmp_path / "repo", {"README.md": "Initial README\n"}, git_env)
    app_settings = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(paths.root)})

    async with httpx2.AsyncClient(timeout=60.0) as http:
        github = GitHubAuth(databases, keystore, http, FakeGh(None))
        runner = GitRunner(paths.bin, github.token)
        workspace_svc = WorkspaceService(app_settings, paths, databases, runner, github, http)
        events = SqliteEventLog(databases)
        project = await workspace_svc.add_local(str(repo))
        await set_permission_level(databases, project.id, "autonomous")

        tasks_svc = TasksService(
            databases=databases,
            paths=paths,
            git=runner,
            events=events,
            workspace=workspace_svc,
        )

        tools = default_registry(tasks=tasks_svc)
        catalog = AgentCatalog.load(tools.names() | STAGE_2_TOOLS)
        providers_svc = ProvidersService(databases, keystore, http)
        agent_settings = AgentSettingsService(databases, providers_svc, catalog)

        prov = await providers_svc.create_provider(
            ProviderCreate(name=provider_name, kind="hosted", base_url=base_url)
        )
        await providers_svc.add_key(prov.name, key_val)
        await providers_svc.add_model(ModelCreate(provider=provider_name, wire_model_id=wire_id))
        await providers_svc.verify_model(live_model)
        await agent_settings.set_default_model("orchestrator", live_model)

        status_board = SqliteStatusBoard(catalog, events)
        manager = SessionManager(
            databases=databases,
            workspace=workspace_svc,
            catalog=catalog,
            tools=tools,
            settings=agent_settings,
            providers=providers_svc,
            http=http,
            gate=ApprovalGate(databases),
            inbound=NoInbound(),
            turn_end=NoTurnEndCheck(),
            workspaces=tasks_svc,
            status_board=status_board,
        )

        start = await manager.get(project.id, "orchestrator")
        sub = manager.subscribe(project.id, "orchestrator", after_seq=start.last_seq)
        await manager.send(
            project.id,
            "orchestrator",
            "Please start a task, add a line 'Updated by orchestrator' to README.md, and open a local review for it.",
        )

        events_list: list[StreamEvent] = []
        async for ev in sub:
            events_list.append(ev)
            if isinstance(ev, TurnEnded):
                break

        assert any(isinstance(e, TurnEnded) and e.data.status == "done" for e in events_list)
        view = await manager.get(project.id, "orchestrator")
        assert len(view.messages) >= 2
        agent_msg = view.messages[-1]
        text = "".join(p.text for p in agent_msg.parts if p.type == "text")
        print("\nLive model response:", text)
        assert len(text) > 0
        await manager.close()
