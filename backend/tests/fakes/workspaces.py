from __future__ import annotations

from typing import TYPE_CHECKING

from mlops_agents.agents.tools.files import RepoFiles

if TYPE_CHECKING:
    from mlops_agents.agents.tools.base import FileAccess, TaskRef
    from mlops_agents.integrations.github.git import GitRunner
    from mlops_agents.workspace.service import WorkspaceService


class FakeWorkspaces:
    """Test double for Workspaces protocol: no task, repository files only."""

    def __init__(self, *, workspace: WorkspaceService, git: GitRunner) -> None:
        self._workspace = workspace
        self._git = git

    async def current(self, project_id: str, session_id: str) -> TaskRef | None:
        return None

    async def files(self, project_id: str, task: TaskRef | None) -> FileAccess:
        if task is not None:
            raise ValueError(f"No task workspaces here, so no files for task {task.id}.")
        project = await self._workspace.get(project_id)
        return RepoFiles(self._workspace.repo_root(project), self._git)


RepositoryOnlyWorkspaces = FakeWorkspaces
