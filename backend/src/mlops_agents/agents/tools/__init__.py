import shutil

from mlops_agents.agents.tools.base import ToolRegistry
from mlops_agents.agents.tools.glob import GlobTool
from mlops_agents.agents.tools.grep import GrepTool
from mlops_agents.agents.tools.read import ReadTool


def default_registry() -> ToolRegistry:
    """The tools agents can be given. No tool needs a service yet; a later plan whose tool
    needs one adds it as a keyword-only parameter here and passes it in `api/services.py`."""
    return ToolRegistry([ReadTool(), GlobTool(), GrepTool(shutil.which("rg"))])
