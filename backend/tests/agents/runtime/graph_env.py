"""The agent, the hooks and the recording sink the runtime tests build graph and executor runs with."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mlops_agents.agents.approvals.gate import ApprovalGate
from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.agent import Agent
from mlops_agents.agents.runtime.contract import MessageView, ToolCallPart, ToolResultPart
from mlops_agents.agents.runtime.graph import TurnHooks
from mlops_agents.agents.tasks.contract import TaskWorkspaces
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.base import FileAccess, TaskRef
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.workspace.models import Project
from tests.agents.runtime.hook_fakes import NoInbound
from tests.fakes.turn_end import NoTurnEndCheck


class FixedWorkspaces:
    """No task, and one given view of the repository: for graphs run without a workspace service."""

    def __init__(self, files: FileAccess) -> None:
        self._files = files

    async def current(self, project_id: str, session_id: str) -> TaskRef | None:
        return None

    async def files(self, project_id: str, task: TaskRef | None) -> FileAccess:
        return self._files


def stage1_hooks(workspaces: TaskWorkspaces) -> TurnHooks:
    """Every call allowed (ApprovalGate defaults to allow for READ tools), nothing delivered mid-turn, no turn-end note."""
    return TurnHooks(
        gate=ApprovalGate(), inbound=NoInbound(), turn_end=NoTurnEndCheck(), workspaces=workspaces
    )


def graph_hooks(agent: Agent) -> TurnHooks:
    """Stage 1's hooks for a graph run over `make_agent`'s repository."""
    return stage1_hooks(FixedWorkspaces(agent.files))


class RecordingSink:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.tools_started: list[ToolCallPart] = []
        self.tools_finished: list[ToolResultPart] = []
        self.messages: list[tuple[MessageView, bool]] = []
        self.waiting_turns: list[str] = []

    async def text(self, message_id: str, text: str) -> None:
        self.texts.append(text)

    async def tool_started(self, message_id: str, call: ToolCallPart) -> None:
        self.tools_started.append(call)

    async def tool_finished(self, message_id: str, result: ToolResultPart) -> None:
        self.tools_finished.append(result)

    async def message(self, message: MessageView, *, created: bool) -> None:
        self.messages.append((message, created))

    async def turn_waiting(self, turn_id: str) -> None:
        self.waiting_turns.append(turn_id)


def make_agent(
    repo_dir: Path,
    bin_dir: Path,
    *,
    project_id: str = "prj_chk",
    max_steps: int | None = 25,
    tool_names: tuple[str, ...] | None = None,
) -> Agent:
    tools = default_registry()
    catalog = AgentCatalog.load(tools.names())
    definition = catalog.get("orchestrator")
    project = Project(
        id=project_id,
        name="test",
        source="local",
        local_path=str(repo_dir),
        github_full_name=None,
        default_branch="main",
        status="ok",
        status_detail=None,
        created_at="2026-10-21T00:00:00Z",
        last_opened_at=None,
    )
    model = ResolvedModel(
        address="fake:fake-tools",
        provider_id="fake",
        provider_name="fake",
        base_url="http://fake",
        wire_model_id="fake-tools",
        context_window=32768,
        keys=(),
    )
    repo_files = RepoFiles(repo_dir, GitRunner(bin_dir))
    agent_tools: list[Any] = []
    for name in tool_names if tool_names is not None else definition.tools:
        t = tools.get(name)
        if t is not None:
            agent_tools.append(t)

    return Agent(
        definition=definition,
        project=project,
        layout="README.md",
        model=model,
        max_steps=max_steps,
        tools=tuple(agent_tools),
        files=repo_files,
    )
