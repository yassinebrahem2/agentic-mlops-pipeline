"""The PR reminder turn-end check (spec §6)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mlops_agents.agents.tasks.contract import Tasks


class PrReminder:
    """Reminds the agent at the end of a turn when its current task has changes not in a PR."""

    def __init__(self, tasks: Tasks) -> None:
        self._tasks = tasks

    async def note(self, project_id: str, session_id: str, turn_id: str) -> str | None:
        task = await self._tasks.current(project_id, session_id)
        if task is None:
            return None

        if not await self._tasks.has_unpublished_changes(project_id, task):
            return None

        task_view = await self._tasks.view(project_id, task.id)
        return (
            f"Your task '{task_view.title}' has changes that are not in a pull request. "
            "If the work is finished, open one with OpenPullRequest. Otherwise say what you are waiting for."
        )
