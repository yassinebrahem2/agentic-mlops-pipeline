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
from mlops_agents.errors import DomainError
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
from tests.agents.runtime.session_env import build_session_env
from tests.conftest import STAGE_2_TOOLS, MemoryKeyStore, make_repo, memory_project_secrets
from tests.fakes.fake_openai import RunningFake
from tests.fakes.turn_end import NoTurnEndCheck
from tests.integrations.test_github_auth import FakeGh

NAME_MAP: dict[str, str] = {
    "Orchestrator": "orchestrator",
    "Onboarding & Diagnosis": "onboarding",
    "Data Quality": "data-quality",
    "Model Performance": "model-performance",
    "Retraining": "retraining",
    "Deployment & Rollback": "deployment",
    "Alert Prioritization": "alert-prioritization",
}


def load_spec_table_data() -> list[dict[str, Any]]:
    spec_path = (
        Path(__file__).resolve().parents[3]
        / "docs/superpowers/specs/2026-10-24-stage-2-agent-runtime-design.md"
    )
    lines = spec_path.read_text(encoding="utf-8").splitlines()
    in_s9 = False
    table_lines: list[str] = []
    for line in lines:
        s = line.strip()
        if s.startswith("## 9. The seven agents"):
            in_s9 = True
            continue
        if in_s9:
            if s.startswith("## "):
                break
            if s.startswith("|"):
                table_lines.append(s)

    data_lines = table_lines[2:]
    if len(data_lines) != 7:
        raise AssertionError(f"Expected exactly 7 rows in spec table, got {len(data_lines)}")

    parsed_rows: list[dict[str, Any]] = []
    for row in data_lines:
        cols = [c.strip() for c in row.split("|")[1:-1]]
        agent_display = cols[0]
        if agent_display not in NAME_MAP:
            raise AssertionError(f"Unmapped agent display name: {agent_display!r}")
        parsed_rows.append(
            {
                "display_name": agent_display,
                "name": NAME_MAP[agent_display],
                "chat": cols[1].lower() == "yes",
                "raw_tools": cols[2],
                "raw_msg": cols[3],
                "schedules": cols[4].lower() == "yes",
                "status_board": (NAME_MAP[agent_display] == "orchestrator"),
            }
        )

    all_agents = [r["name"] for r in parsed_rows]
    chat_agents = [r["name"] for r in parsed_rows if r["chat"]]

    result: list[dict[str, Any]] = []
    prev_tools: tuple[str, ...] = ()
    for r in parsed_rows:
        raw_tools = r["raw_tools"]
        if raw_tools.lower() == "same":
            tools = prev_tools
        elif "all general tools" in raw_tools.lower():
            base = ("Read", "Glob", "Grep", "Write", "Edit", "Bash", "StartTask", "ResumeTask")
            rest = (
                "OpenPullRequest",
                "SearchIssues",
                "OpenIssue",
                "CommentOnIssue",
                "ListAgents",
                "SendMessage",
            )
            tools = base + rest + (("ScheduleWakeup",) if r["schedules"] else ())
        else:
            tools = tuple(t.strip() for t in raw_tools.split(",") if t.strip())

        if not tools or tools[0] != "Read":
            raise AssertionError(f"Tools for {r['name']} must start with 'Read', got {tools}")
        prev_tools = tools

        raw_msg = r["raw_msg"]
        if raw_msg == "all but Alert Prioritization":
            can_msg = tuple(a for a in all_agents if a not in ("alert-prioritization", r["name"]))
        elif raw_msg == "all chat agents":
            can_msg = tuple(a for a in chat_agents if a != r["name"])
        else:
            for p in raw_msg.split(","):
                p_clean = p.strip()
                if p_clean and p_clean not in NAME_MAP:
                    raise AssertionError(f"Unmapped agent in May message: {p_clean!r}")
            can_msg = tuple(NAME_MAP[p.strip()] for p in raw_msg.split(",") if p.strip())
        result.append({**r, "tools": tools, "can_message": can_msg})
    return result


SPEC_ROWS = load_spec_table_data()


def _load_definition(name: str) -> AgentDefinition:
    folder = files("mlops_agents.agents").joinpath("definitions")
    entry = folder.joinpath(f"{name}.md")
    return parse_definition(entry.read_text(encoding="utf-8"), f"{name}.md")


def _assert_definition_matches_spec(defn: AgentDefinition, expected: dict[str, Any]) -> None:
    assert defn.name == expected["name"] and defn.display_name == expected["display_name"]
    assert defn.chat is expected["chat"] and defn.schedules is expected["schedules"]
    assert defn.status_board is expected["status_board"]
    assert defn.tools[0] == "Read" and defn.tools == expected["tools"]
    assert defn.can_message == expected["can_message"]


@pytest.mark.parametrize("expected", SPEC_ROWS, ids=[r["name"] for r in SPEC_ROWS])
def test_stage2_agent_definitions_match_spec(expected: dict[str, Any]) -> None:
    defn = _load_definition(expected["name"])
    _assert_definition_matches_spec(defn, expected)
    assert len(defn.prompt.splitlines()) < 60, f"{defn.name} prompt exceeds 60 lines"
    low = defn.prompt.lower()
    assert "how to work:" in low or "how to work" in low, f"{defn.name} missing 'How to work'"
    if defn.chat:
        assert "starttask" in low, f"{defn.name} missing StartTask guidance"
    else:
        assert "monitoring loop" in low or "stage 4" in low, f"{defn.name} missing loop guidance"
    assert "not available yet" in low, f"{defn.name} missing 'not available yet'"


def test_spec_table_detects_missing_tool() -> None:
    expected = SPEC_ROWS[0]
    defn = _load_definition(expected["name"])
    tampered = defn.model_copy(update={"tools": defn.tools[:-1]})
    with pytest.raises(AssertionError):
        _assert_definition_matches_spec(tampered, expected)


def test_agent_catalog_loads_stage2_definitions_with_full_tools() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS)
    assert catalog.get("orchestrator").status_board is True
    for name in ("onboarding", "data-quality", "model-performance", "retraining", "deployment"):
        defn = catalog.get(name)
        assert not defn.status_board and defn.chat and defn.schedules

    alert_defn = catalog.get("alert-prioritization")
    alert_spec = next(r for r in SPEC_ROWS if r["name"] == "alert-prioritization")
    assert not alert_defn.status_board and not alert_defn.chat and not alert_defn.schedules
    assert alert_defn.tools == alert_spec["tools"]


def test_alert_prioritization_loads_in_catalog() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS)
    defn = catalog.get("alert-prioritization")
    alert_spec = next(r for r in SPEC_ROWS if r["name"] == "alert-prioritization")
    assert defn.name == "alert-prioritization" and defn.display_name == "Alert Prioritization"
    assert not defn.chat and not defn.schedules and not defn.status_board
    assert defn.tools == alert_spec["tools"] and defn.can_message == alert_spec["can_message"]


async def test_alert_prioritization_derived_as_inactive_on_status_board(
    databases: Databases,
) -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS)
    events = SqliteEventLog(databases)
    board = SqliteStatusBoard(catalog, events)
    statuses = await board.snapshot("prj_test123")
    alert_status = next(s for s in statuses if s.agent == "alert-prioritization")
    assert alert_status.state == "inactive"
    assert alert_status.display_name == "Alert Prioritization"
    summary = await board.summary("prj_test123")
    assert "Alert Prioritization: inactive" in summary


async def test_alert_prioritization_cannot_chat(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    with pytest.raises(DomainError) as exc_info:
        await env.manager.send(env.project_id, "alert-prioritization", "Hello")
    assert exc_info.value.code == "agent_cannot_chat"
    assert exc_info.value.status == 409
    await env.manager.close()


def test_no_stage2_agent_may_message_alert_prioritization() -> None:
    catalog = AgentCatalog.load(STAGE_2_TOOLS)
    for defn in catalog.all():
        assert "alert-prioritization" not in defn.can_message, (
            f"Agent '{defn.name}' unexpectedly allows messaging alert-prioritization"
        )


def test_agent_catalog_fails_when_messaging_tools_missing() -> None:
    with pytest.raises(DefinitionError) as exc_info:
        AgentCatalog.load(default_registry().names())
    error_msg = str(exc_info.value)
    assert "ListAgents" in error_msg and "SendMessage" in error_msg

    with pytest.raises(DefinitionError) as exc_info_sched:
        AgentCatalog.load(STAGE_2_TOOLS - {"ScheduleWakeup"})
    assert "ScheduleWakeup" in str(exc_info_sched.value)


def _load_local_env() -> None:
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}


@pytest.mark.live
@pytest.mark.timeout(300)
async def test_live_agent_definitions(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    _load_local_env()
    live_model = os.environ.get("MLOPS_AGENTS_LIVE_MODEL")
    if not live_model:
        pytest.skip("set MLOPS_AGENTS_LIVE_MODEL to run this live test")

    provider_name, separator, wire_id = live_model.partition(":")
    if not separator or not wire_id:
        pytest.fail(
            "MLOPS_AGENTS_LIVE_MODEL must be 'provider:model', e.g. groq:llama-3.3-70b-versatile"
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

    async with httpx2.AsyncClient(timeout=120.0) as http:
        github = GitHubAuth(databases, keystore, http, FakeGh(None))
        runner = GitRunner(paths.bin, github.token)
        workspace_svc = WorkspaceService(
            app_settings, paths, databases, runner, github, http, memory_project_secrets(databases)
        )
        events = SqliteEventLog(databases)
        project = await workspace_svc.add_local(str(repo))
        await set_permission_level(databases, project.id, "autonomous")

        tasks_svc = TasksService(
            databases=databases, paths=paths, git=runner, events=events, workspace=workspace_svc
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

        chat_agents = [d.name for d in catalog.all() if d.chat]
        for agent in chat_agents:
            await agent_settings.set_default_model(agent, live_model)

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
            status_board=SqliteStatusBoard(catalog, events),
        )

        for agent in chat_agents:
            start = await manager.get(project.id, agent)
            sub = manager.subscribe(project.id, agent, after_seq=start.last_seq)
            prompt = (
                "Please start a task, add a line 'Updated by orchestrator' to README.md, and open a local review for it."
                if agent == "orchestrator"
                else f"Hello {agent}, please reply with a short greeting and introduce your role in one sentence."
            )
            await manager.send(project.id, agent, prompt)

            events_list: list[StreamEvent] = []
            async for ev in sub:
                events_list.append(ev)
                if isinstance(ev, TurnEnded):
                    break

            assert any(isinstance(e, TurnEnded) and e.data.status == "done" for e in events_list)
            view = await manager.get(project.id, agent)
            assert len(view.messages) >= 2
            agent_msg = view.messages[-1]
            text = "".join(p.text for p in agent_msg.parts if p.type == "text")
            print(f"\nLive model response from {agent}:\n{text}\n")
            assert len(text) > 0

        # Tool-using agent called one of its own tools
        orch_defn = catalog.get("orchestrator")
        orch_view = await manager.get(project.id, "orchestrator")
        has_tool_call = any(
            p.type == "tool_call" and p.name in orch_defn.tools
            for m in orch_view.messages
            for p in m.parts
        )
        assert has_tool_call, "Orchestrator did not call any of its own tools"

        # Task was started
        task = await tasks_svc.current(project.id, "orchestrator")
        assert task is not None, "Task was not started"

        # README.md was changed in the task's worktree
        worktree_readme = (task.worktree / "README.md").read_text(encoding="utf-8")
        assert "Updated by orchestrator" in worktree_readme

        # A local review was opened in the Inbox / tasks service
        task_view = await tasks_svc.view(project.id, task.id)
        assert task_view.status == "in_review" and task_view.review_kind == "local"
        open_tasks = await tasks_svc.open_tasks(project.id)
        assert any(
            t.id == task.id and t.status == "in_review" and t.review_kind == "local"
            for t in open_tasks
        )

        await manager.close()
