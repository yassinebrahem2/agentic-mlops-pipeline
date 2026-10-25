from __future__ import annotations

import shutil
from typing import Any

from mlops_agents.agents.tools.base import AgentTool, ToolRegistry
from mlops_agents.agents.tools.bash import BashTool
from mlops_agents.agents.tools.glob import GlobTool
from mlops_agents.agents.tools.grep import GrepTool
from mlops_agents.agents.tools.read import ReadTool
from mlops_agents.sandbox.contract import Sandbox


def default_registry(*, sandbox: Sandbox | None = None) -> ToolRegistry:
    """The tools agents can be given. Includes BashTool when sandbox is provided."""
    tools: list[AgentTool[Any]] = [ReadTool(), GlobTool(), GrepTool(shutil.which("rg"))]
    if sandbox is not None:
        tools.append(BashTool(sandbox=sandbox))
    return ToolRegistry(tools)
