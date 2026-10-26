from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, Any

from mlops_agents.agents.tools.base import AgentTool, ToolRegistry
from mlops_agents.agents.tools.bash import BashTool
from mlops_agents.agents.tools.edit import EditTool
from mlops_agents.agents.tools.glob import GlobTool
from mlops_agents.agents.tools.grep import GrepTool
from mlops_agents.agents.tools.issues import (
    CommentOnIssueTool,
    OpenIssueTool,
    SearchIssuesTool,
)
from mlops_agents.agents.tools.pull_request import OpenPullRequestTool
from mlops_agents.agents.tools.read import ReadTool
from mlops_agents.agents.tools.tasks import ResumeTaskTool, StartTaskTool
from mlops_agents.agents.tools.write import WriteTool

if TYPE_CHECKING:
    import httpx2

    from mlops_agents.agents.tasks.contract import Tasks
    from mlops_agents.events.contract import EventLog
    from mlops_agents.integrations.github.auth import GitHubAuth
    from mlops_agents.sandbox.contract import Sandbox
    from mlops_agents.storage.databases import Databases
    from mlops_agents.workspace.service import WorkspaceService

__all__ = [
    "BashTool",
    "CommentOnIssueTool",
    "EditTool",
    "GlobTool",
    "GrepTool",
    "OpenIssueTool",
    "OpenPullRequestTool",
    "ReadTool",
    "ResumeTaskTool",
    "SearchIssuesTool",
    "StartTaskTool",
    "ToolRegistry",
    "WriteTool",
    "default_registry",
]


def default_registry(
    *,
    tasks: Tasks | None = None,
    sandbox: Sandbox | None = None,
    workspace: WorkspaceService | None = None,
    github: GitHubAuth | None = None,
    events: EventLog | None = None,
    http: httpx2.AsyncClient | None = None,
    databases: Databases | None = None,
) -> ToolRegistry:
    """The tools agents can be given. No tool needs a service yet; a later plan whose tool
    needs one adds it as a keyword-only parameter here and passes it in `api/services.py`."""
    tools: list[AgentTool[Any]] = [ReadTool(tasks=tasks), GlobTool(), GrepTool(shutil.which("rg"))]
    if tasks is not None:
        effective_sandbox = sandbox if sandbox is not None else getattr(tasks, "sandbox", None)
        tools.extend(
            [
                StartTaskTool(tasks),
                ResumeTaskTool(tasks),
                WriteTool(tasks, sandbox=effective_sandbox),
                EditTool(tasks, sandbox=effective_sandbox),
            ]
        )
    if sandbox is not None:
        tools.append(BashTool(sandbox=sandbox))
    if (
        tasks is not None
        and workspace is not None
        and github is not None
        and events is not None
        and http is not None
    ):
        tools.append(OpenPullRequestTool(tasks, workspace, github, events, http))
    if (
        tasks is not None
        and github is not None
        and events is not None
        and http is not None
        and databases is not None
    ):
        tools.extend(
            [
                SearchIssuesTool(tasks, github, http),
                OpenIssueTool(tasks, github, events, http, databases),
                CommentOnIssueTool(tasks, github, events, http),
            ]
        )
    return ToolRegistry(tools)
