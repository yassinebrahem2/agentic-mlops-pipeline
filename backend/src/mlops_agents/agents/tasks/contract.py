"""Task workspaces and the task service (I12)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from mlops_agents.agents.runtime.contract import TaskView
from mlops_agents.agents.tools.base import FileAccess, TaskRef


class ClaimConflict(Exception):
    """A path is claimed by another open task. Its message names the other agent and task."""

    def __init__(self, message: str, *, agent: str, task_id: str) -> None:
        super().__init__(message)
        self.agent = agent
        self.task_id = task_id


class CommitRefused(Exception):
    """Its message is written for the model."""


@dataclass(frozen=True)
class CommitResult:
    sha: str
    changed: tuple[str, ...]


class TaskWorkspaces(Protocol):
    async def current(self, project_id: str, session_id: str) -> TaskRef | None: ...

    async def files(self, project_id: str, task: TaskRef | None) -> FileAccess: ...


class Tasks(TaskWorkspaces, Protocol):
    async def start(self, project_id: str, session_id: str, agent: str, title: str) -> TaskRef: ...

    async def resume(
        self, project_id: str, session_id: str, agent: str, task_id: str
    ) -> TaskRef: ...

    async def view(self, project_id: str, task_id: str) -> TaskView: ...

    async def open_tasks(self, project_id: str) -> list[TaskView]: ...

    async def record_read(self, project_id: str, task: TaskRef, path: str, sha256: str) -> None: ...

    async def read_hash(self, project_id: str, task: TaskRef, path: str) -> str | None: ...

    async def claim(self, project_id: str, task: TaskRef, paths: Sequence[str]) -> None:
        """Raises ClaimConflict."""
        ...

    async def has_unpublished_changes(self, project_id: str, task: TaskRef) -> bool: ...

    async def staged_diff(self, project_id: str, task: TaskRef) -> str:
        """What commit would record, capped."""
        ...

    async def commit(self, project_id: str, task: TaskRef, message: str) -> CommitResult:
        """Raises CommitRefused."""
        ...

    async def push(self, project_id: str, task: TaskRef) -> None: ...

    async def local_merge_preview(self, project_id: str, task: TaskRef) -> list[str]:
        """The user files that change."""
        ...

    async def local_merge(self, project_id: str, task: TaskRef) -> None:
        """Spec section 6, steps 1 to 3. Raises CommitRefused."""
        ...

    async def set_review(
        self,
        project_id: str,
        task_id: str,
        *,
        kind: Literal["github", "local"],
        pr_number: int | None = None,
        pr_url: str | None = None,
    ) -> TaskView: ...

    async def end(
        self, project_id: str, task_id: str, status: Literal["merged", "closed", "discarded"]
    ) -> None: ...
