from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict

from mlops_agents.agents.tools.base import (
    AgentTool,
    RiskClass,
    ToolContext,
    ToolOutput,
)
from mlops_agents.errors import DomainError

if TYPE_CHECKING:
    from mlops_agents.agents.catalog import AgentCatalog, AgentDefinition
    from mlops_agents.events.contract import AgentStatus, StatusBoard


class ListAgentsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListAgentsTool(AgentTool[ListAgentsArgs]):
    name: ClassVar[str] = "ListAgents"
    description: ClassVar[str] = (
        "List all agents in the project with their status, current task, open PRs, "
        "and whether you may send them a message."
    )
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = ListAgentsArgs

    def __init__(
        self,
        catalog: AgentCatalog | None = None,
        status_board: StatusBoard | None = None,
    ) -> None:
        self._catalog = catalog
        self._status_board = status_board

    def bind(self, catalog: AgentCatalog, status_board: StatusBoard) -> None:
        self._catalog = catalog
        self._status_board = status_board

    async def run(self, ctx: ToolContext, args: ListAgentsArgs) -> ToolOutput:
        if self._catalog is None or self._status_board is None:
            raise RuntimeError("ListAgentsTool has not been bound with catalog and status_board")
        caller_def: AgentDefinition | None = None
        with contextlib.suppress(DomainError):
            caller_def = self._catalog.get(ctx.agent)

        snapshots = await self._status_board.snapshot(ctx.project_id)

        seen: set[str] = set()
        items: list[tuple[str, AgentDefinition | None, AgentStatus | None]] = []
        for s in snapshots:
            defn: AgentDefinition | None = None
            with contextlib.suppress(DomainError):
                defn = self._catalog.get(s.agent)
            items.append((s.agent, defn, s))
            seen.add(s.agent)

        for d in self._catalog.all():
            if d.name not in seen:
                items.append((d.name, d, None))
                seen.add(d.name)

        blocks: list[str] = []
        for name, defn, status in items:
            display_name = defn.display_name if defn else (status.display_name if status else name)
            description = defn.description if defn else ""
            chats = "yes" if (defn.chat if defn else False) else "no"

            if status is not None:
                state = status.state
                task = status.task.title if status.task is not None else "none"
                if status.open_prs:
                    prs = ", ".join(
                        f"#{pr.number}: {pr.title}" if pr.title else f"#{pr.number}"
                        for pr in status.open_prs
                    )
                else:
                    prs = "none"
            else:
                state = "inactive" if (defn and not defn.chat) else "idle"
                task = "none"
                prs = "none"

            may_message = (
                "yes" if (caller_def is not None and name in caller_def.can_message) else "no"
            )

            block = (
                f"Name: {name}\n"
                f"Display name: {display_name}\n"
                f"Description: {description}\n"
                f"Chats: {chats}\n"
                f"State: {state}\n"
                f"Current task: {task}\n"
                f"Open PRs: {prs}\n"
                f"May message: {may_message}"
            )
            blocks.append(block)

        return ToolOutput("\n\n".join(blocks))
