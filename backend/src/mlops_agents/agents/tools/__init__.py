from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, Any

from mlops_agents.agents.tools.base import AgentTool, ToolRegistry
from mlops_agents.agents.tools.bash import BashTool
from mlops_agents.agents.tools.edit import EditTool
from mlops_agents.agents.tools.glob import GlobTool
from mlops_agents.agents.tools.grep import GrepTool
from mlops_agents.agents.tools.list_agents import ListAgentsTool
from mlops_agents.agents.tools.read import ReadTool
from mlops_agents.agents.tools.schedule_wakeup import ScheduleWakeupTool
from mlops_agents.agents.tools.tasks import ResumeTaskTool, StartTaskTool
from mlops_agents.agents.tools.write import WriteTool

if TYPE_CHECKING:
    from mlops_agents.agents.catalog import AgentCatalog
    from mlops_agents.agents.messaging.bus import SqliteMessageBus
    from mlops_agents.agents.messaging.wakeups import WakeupService
    from mlops_agents.agents.tasks.contract import Tasks
    from mlops_agents.events.contract import StatusBoard
    from mlops_agents.sandbox.contract import Sandbox

__all__ = [
    "BashTool",
    "EditTool",
    "GlobTool",
    "GrepTool",
    "ListAgentsTool",
    "ReadTool",
    "ResumeTaskTool",
    "ScheduleWakeupTool",
    "StartTaskTool",
    "ToolRegistry",
    "WriteTool",
    "default_registry",
]


def default_registry(
    *,
    tasks: Tasks | None = None,
    sandbox: Sandbox | None = None,
    bus: SqliteMessageBus | None = None,
    status_board: StatusBoard | None = None,
    catalog: AgentCatalog | None = None,
    wakeups: WakeupService | None = None,
) -> ToolRegistry:
    """The tools agents can be given. Tools that need services take them here as keyword-only
    parameters, and `api/services.py` passes them in."""
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
    registry = ToolRegistry(tools)
    if bus is not None:
        # Imported here: the bus reaches this package through the session contract, so a
        # top-level import would be circular.
        from mlops_agents.agents.tools.send_message import SendMessageTool

        registry.register(SendMessageTool(bus))
    if status_board is not None:
        effective_catalog = (
            catalog if catalog is not None else getattr(status_board, "_catalog", None)
        )
        if effective_catalog is not None:
            registry.register(ListAgentsTool(catalog=effective_catalog, status_board=status_board))
    if wakeups is not None:
        effective_catalog = catalog if catalog is not None else getattr(wakeups, "catalog", None)
        registry.register(ScheduleWakeupTool(wakeups, catalog=effective_catalog))
    return registry
