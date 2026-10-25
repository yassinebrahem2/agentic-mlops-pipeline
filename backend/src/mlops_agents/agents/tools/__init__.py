from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, Any

from mlops_agents.agents.tools.base import AgentTool, ToolRegistry
from mlops_agents.agents.tools.edit import EditTool
from mlops_agents.agents.tools.glob import GlobTool
from mlops_agents.agents.tools.grep import GrepTool
from mlops_agents.agents.tools.read import ReadTool
from mlops_agents.agents.tools.tasks import ResumeTaskTool, StartTaskTool
from mlops_agents.agents.tools.write import WriteTool

if TYPE_CHECKING:
    from mlops_agents.agents.tasks.contract import Tasks

__all__ = [
    "EditTool",
    "GlobTool",
    "GrepTool",
    "ReadTool",
    "ResumeTaskTool",
    "StartTaskTool",
    "ToolRegistry",
    "WriteTool",
    "default_registry",
]


def default_registry(*, tasks: Tasks | None = None) -> ToolRegistry:
    """The tools agents can be given. No tool needs a service yet; a later plan whose tool
    needs one adds it as a keyword-only parameter here and passes it in `api/services.py`."""
    tools: list[AgentTool[Any]] = [ReadTool(tasks=tasks), GlobTool(), GrepTool(shutil.which("rg"))]
    if tasks is not None:
        tools.extend(
            [
                StartTaskTool(tasks),
                ResumeTaskTool(tasks),
                WriteTool(tasks),
                EditTool(tasks),
            ]
        )
    return ToolRegistry(tools)
