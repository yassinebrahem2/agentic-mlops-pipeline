import shutil

from mlops_agents.agents.tools.base import ToolRegistry
from mlops_agents.agents.tools.glob import GlobTool
from mlops_agents.agents.tools.grep import GrepTool
from mlops_agents.agents.tools.read import ReadTool


def default_registry() -> ToolRegistry:
    """The tools agents can be given. A tool that needs a service receives it in its
    constructor; the service comes in here as a keyword-only parameter, which
    `api/services.py` passes. No tool needs one yet."""
    return ToolRegistry([ReadTool(), GlobTool(), GrepTool(shutil.which("rg"))])
