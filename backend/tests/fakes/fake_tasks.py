"""An in-memory Tasks double: it reports a fixed list of open tasks; the rest is never used."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Literal

from mlops_agents.agents.runtime.contract import TaskView
from mlops_agents.agents.tasks.contract import CommitResult, Tasks
from mlops_agents.agents.tools.base import FileAccess, TaskRef
from mlops_agents.workspace.refs import RepoRef

_UNUSED = "FakeTasks only implements open_tasks."


class FakeTasks(Tasks):
    def __init__(self, open_tasks: list[TaskView] | None = None) -> None:
        self._open_tasks = open_tasks or []

    async def open_tasks(self, project_id: str) -> list[TaskView]:
        return list(self._open_tasks)

    async def current(self, project_id: str, session_id: str) -> TaskRef | None:
        raise NotImplementedError(_UNUSED)

    async def files(self, project_id: str, task: TaskRef | None) -> FileAccess:
        raise NotImplementedError(_UNUSED)

    async def start(self, project_id: str, session_id: str, agent: str, title: str) -> TaskRef:
        raise NotImplementedError(_UNUSED)

    async def resume(self, project_id: str, session_id: str, agent: str, task_id: str) -> TaskRef:
        raise NotImplementedError(_UNUSED)

    async def view(self, project_id: str, task_id: str) -> TaskView:
        raise NotImplementedError(_UNUSED)

    async def record_read(self, project_id: str, task: TaskRef, path: str, sha256: str) -> None:
        raise NotImplementedError(_UNUSED)

    async def read_hash(self, project_id: str, task: TaskRef, path: str) -> str | None:
        raise NotImplementedError(_UNUSED)

    async def claim(self, project_id: str, task: TaskRef, paths: Sequence[str]) -> None:
        raise NotImplementedError(_UNUSED)

    async def repo_ref(self, project_id: str) -> RepoRef | None:
        raise NotImplementedError(_UNUSED)

    async def has_commits_beyond_base(self, project_id: str, task: TaskRef) -> bool:
        raise NotImplementedError(_UNUSED)

    async def decide_review(
        self,
        project_id: str,
        task_id: str,
        step: Callable[[TaskRef], Awaitable[None]],
        status: Literal["merged", "closed"],
    ) -> TaskView:
        raise NotImplementedError(_UNUSED)

    async def has_unpublished_changes(self, project_id: str, task: TaskRef) -> bool:
        raise NotImplementedError(_UNUSED)

    async def staged_diff(self, project_id: str, task: TaskRef) -> str:
        raise NotImplementedError(_UNUSED)

    async def commit(self, project_id: str, task: TaskRef, message: str) -> CommitResult:
        raise NotImplementedError(_UNUSED)

    async def push(self, project_id: str, task: TaskRef) -> None:
        raise NotImplementedError(_UNUSED)

    async def local_merge_preview(self, project_id: str, task: TaskRef) -> list[str]:
        raise NotImplementedError(_UNUSED)

    async def local_merge(self, project_id: str, task: TaskRef) -> None:
        raise NotImplementedError(_UNUSED)

    async def set_review(
        self,
        project_id: str,
        task_id: str,
        *,
        kind: Literal["github", "local"],
        pr_number: int | None = None,
        pr_url: str | None = None,
    ) -> TaskView:
        raise NotImplementedError(_UNUSED)

    async def end(
        self, project_id: str, task_id: str, status: Literal["merged", "closed", "discarded"]
    ) -> None:
        raise NotImplementedError(_UNUSED)
