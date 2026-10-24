"""The agent and the recording sink the runtime tests build their graph and executor runs with."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.agents.runtime.agent import Agent
from mlops_agents.agents.runtime.contract import MessageView, ToolCallPart, ToolResultPart
from mlops_agents.agents.tools import default_registry
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.workspace.models import Project
from tests.agents.ctx import tool_context


class RecordingSink:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.tools_started: list[ToolCallPart] = []
        self.tools_finished: list[ToolResultPart] = []
        self.messages: list[tuple[MessageView, bool]] = []

    async def text(self, message_id: str, text: str) -> None:
        self.texts.append(text)

    async def tool_started(self, message_id: str, call: ToolCallPart) -> None:
        self.tools_started.append(call)

    async def tool_finished(self, message_id: str, result: ToolResultPart) -> None:
        self.tools_finished.append(result)

    async def message(self, message: MessageView, *, created: bool) -> None:
        self.messages.append((message, created))


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
    tool_ctx = tool_context(repo_files)
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
        tool_context=tool_ctx,
    )
