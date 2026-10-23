from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from mlops_agents.agents.catalog import AgentCatalog, AgentDefinition
from mlops_agents.agents.runtime.context import top_level_layout
from mlops_agents.agents.settings import AgentSettingsService
from mlops_agents.agents.tools.base import AgentTool, ToolContext, ToolRegistry
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.credentials.keystore import KeyStoreError
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.providers.bridge import ResolvedModel
from mlops_agents.workspace.models import Project
from mlops_agents.workspace.service import WorkspaceService


@dataclass(frozen=True)
class Agent:
    definition: AgentDefinition
    project: Project
    layout: str  # top_level_layout of the repository
    model: ResolvedModel
    max_steps: int | None
    tools: tuple[AgentTool[Any], ...]  # exactly the definition's tools, in its order
    tool_context: ToolContext  # one RepoFiles per turn (it caches git ls-files)


class TurnRefused(Exception):
    def __init__(self, end: Literal["done", "failed"], text: str) -> None:
        super().__init__(text)
        self.end: Literal["done", "failed"] = end
        self.text: str = text


class AgentBuilder:
    def __init__(
        self,
        *,
        catalog: AgentCatalog,
        tools: ToolRegistry,
        settings: AgentSettingsService,
        workspace: WorkspaceService,
        git: GitRunner,
    ) -> None:
        self._catalog = catalog
        self._tools = tools
        self._settings = settings
        self._workspace = workspace
        self._git = git

    async def build(self, project_id: str, agent: str) -> Agent:
        project = await self._workspace.get(project_id)
        if project.status in ("missing", "clone_failed"):
            detail = project.status_detail or f"Project is {project.status}."
            raise TurnRefused("failed", detail)
        if project.status == "empty":
            raise TurnRefused(
                "done",
                "This repository has no committed files yet. Commit your files, then ask again.",
            )

        try:
            resolved_model, max_steps = await self._settings.resolve(project_id, agent)
        except DomainError as exc:
            raise TurnRefused("failed", exc.message) from exc
        except KeyStoreError as exc:
            raise TurnRefused("failed", str(exc)) from exc

        definition = self._catalog.get(agent)
        repo_root = self._workspace.repo_root(project)
        files = RepoFiles(repo_root, self._git)
        tool_context = ToolContext(files=files)
        layout = await top_level_layout(files)

        agent_tools: list[AgentTool[Any]] = []
        for tool_name in definition.tools:
            t = self._tools.get(tool_name)
            if t is not None:
                agent_tools.append(t)

        return Agent(
            definition=definition,
            project=project,
            layout=layout,
            model=resolved_model,
            max_steps=max_steps,
            tools=tuple(agent_tools),
            tool_context=tool_context,
        )
