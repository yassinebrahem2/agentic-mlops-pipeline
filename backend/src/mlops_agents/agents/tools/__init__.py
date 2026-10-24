from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from mlops_agents.agents.tools.base import ToolRegistry
from mlops_agents.agents.tools.glob import GlobTool
from mlops_agents.agents.tools.grep import GrepTool
from mlops_agents.agents.tools.read import ReadTool

if TYPE_CHECKING:
    from mlops_agents.agents.messaging.bus import SqliteMessageBus


def default_registry(*, bus: SqliteMessageBus | None = None) -> ToolRegistry:
    """The tools agents can be given. A tool that needs a service takes it here as a keyword-only
    parameter, and `api/services.py` passes it in. Without the bus there is no SendMessage."""
    registry = ToolRegistry([ReadTool(), GlobTool(), GrepTool(shutil.which("rg"))])
    if bus is not None:
        # Imported here: the bus reaches this package through the session contract, so a
        # top-level import would be circular.
        from mlops_agents.agents.tools.send_message import SendMessageTool

        registry.register(SendMessageTool(bus))
    return registry
