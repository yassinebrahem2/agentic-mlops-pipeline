import shutil

from mlops_agents.agents.tools.base import ToolRegistry
from mlops_agents.agents.tools.glob import GlobTool
from mlops_agents.agents.tools.grep import GrepTool
from mlops_agents.agents.tools.read import ReadTool


def default_registry() -> ToolRegistry:
    return ToolRegistry([ReadTool(), GlobTool(), GrepTool(shutil.which("rg"))])
