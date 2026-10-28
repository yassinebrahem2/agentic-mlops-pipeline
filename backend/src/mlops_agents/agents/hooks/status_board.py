"""The `status_board` hook: the stage 2 status summary, rendered as a context hook."""

from __future__ import annotations

from mlops_agents.agents.hooks.contract import HookContext
from mlops_agents.agents.hooks.names import STATUS_BOARD_HOOK
from mlops_agents.events.contract import StatusBoard


class StatusBoardHook:
    name: str = STATUS_BOARD_HOOK

    def __init__(self) -> None:
        self._board: StatusBoard | None = None

    def bind(self, board: StatusBoard) -> None:
        """The board needs the catalog, which needs the registry's names: bound after both."""
        self._board = board

    async def render(self, ctx: HookContext) -> str:
        if self._board is None:
            return f"[{self.name}: unavailable (not bound)]"
        summary = await self._board.summary(ctx.project_id)
        return f"Status board:\n{summary}" if summary else ""
