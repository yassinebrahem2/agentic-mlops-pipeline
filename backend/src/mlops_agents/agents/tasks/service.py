from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from mlops_agents.agents.runtime.contract import TaskView
from mlops_agents.agents.tasks import queries
from mlops_agents.agents.tasks.claims import claim_paths, get_task_read_hash, record_task_read
from mlops_agents.agents.tasks.commit import (
    commit_task,
    has_commits_beyond_base_task,
    has_unpublished_changes_task,
    push_task,
    staged_diff_task,
)
from mlops_agents.agents.tasks.contract import CommitResult, Tasks
from mlops_agents.agents.tasks.git import git_dir_for_task
from mlops_agents.agents.tasks.lifecycle import (
    WORKTREE_RECREATED_NOTE,
    end_task,
    prepare_worktree,
    resume_task,
    set_review_task,
    start_task,
)
from mlops_agents.agents.tasks.local_merge import local_merge, local_merge_preview
from mlops_agents.agents.tasks.remote import resolve_repo_ref
from mlops_agents.agents.tasks.worktrees import TaskFiles
from mlops_agents.agents.tools.base import FileAccess, TaskRef
from mlops_agents.agents.tools.file_ops import pause_task
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog
from mlops_agents.ids import new_id
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.workspace.service import WorkspaceService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from mlops_agents.sandbox.contract import Sandbox
    from mlops_agents.workspace.refs import RepoRef


class TasksService(Tasks):
    def __init__(
        self,
        *,
        databases: Databases,
        paths: DataPaths,
        git: GitRunner,
        events: EventLog,
        workspace: WorkspaceService,
        sandbox: Sandbox | None = None,
    ) -> None:
        self._databases = databases
        self._paths = paths
        self._git = git
        self._events = events
        self._workspace = workspace
        self._sandbox = sandbox
        self._current_tasks: dict[tuple[str, str], str] = {}
        self._pending_notes: dict[tuple[str, str], str] = {}
        self._task_locks: dict[tuple[str, str], asyncio.Lock] = {}

    def pop_pending_note(self, project_id: str, task_id: str) -> str | None:
        return self._pending_notes.pop((project_id, task_id), None)

    def _task_lock(self, project_id: str, task_id: str) -> asyncio.Lock:
        return self._task_locks.setdefault((project_id, task_id), asyncio.Lock())

    @property
    def sandbox(self) -> Sandbox | None:
        return self._sandbox

    async def start(
        self,
        project_id: str,
        session_id: str,
        agent: str,
        title: str,
    ) -> TaskRef:
        task_id = new_id("tsk")
        project = await self._workspace.get(project_id)
        ref = await start_task(
            runner=self._git,
            project=project,
            repo_root=self._workspace.repo_root(project),
            worktree_path=self._paths.worktree_path(project_id, task_id),
            empty_hooks=self._paths.empty_hooks,
            engine=await self._databases.project(project_id),
            events=self._events,
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
            agent=agent,
            title=title,
        )
        self._current_tasks[(project_id, session_id)] = task_id
        return ref

    async def resume(
        self,
        project_id: str,
        session_id: str,
        agent: str,
        task_id: str,
    ) -> TaskRef:
        ref, old_sid = await resume_task(
            engine=await self._databases.project(project_id),
            events=self._events,
            project_id=project_id,
            session_id=session_id,
            agent=agent,
            task_id=task_id,
        )
        if old_sid and old_sid != session_id:
            self._current_tasks.pop((project_id, old_sid), None)
        self._current_tasks[(project_id, session_id)] = task_id
        return ref

    async def current(self, project_id: str, session_id: str) -> TaskRef | None:
        return await queries.current(
            await self._databases.project(project_id),
            project_id,
            session_id,
            self._current_tasks,
            self._pending_notes,
        )

    async def view(self, project_id: str, task_id: str) -> TaskView:
        return await queries.view(await self._databases.project(project_id), task_id)

    async def record_read(self, project_id: str, task: TaskRef, path: str, sha256: str) -> None:
        await record_task_read(await self._databases.project(project_id), task.id, path, sha256)

    async def read_hash(self, project_id: str, task: TaskRef, path: str) -> str | None:
        return await get_task_read_hash(await self._databases.project(project_id), task.id, path)

    async def claim(self, project_id: str, task: TaskRef, paths: Sequence[str]) -> None:
        engine = await self._databases.project(project_id)
        await claim_paths(engine, task, paths)

    async def open_tasks(self, project_id: str) -> list[TaskView]:
        return await queries.open_tasks(await self._databases.project(project_id))

    async def files(self, project_id: str, task: TaskRef | None) -> FileAccess:
        project = await self._workspace.get(project_id)
        repo_root = self._workspace.repo_root(project)
        if task is None:
            return RepoFiles(repo_root, self._git)
        if await prepare_worktree(
            runner=self._git,
            repo_root=repo_root,
            worktree_path=task.worktree,
            empty_hooks=self._paths.empty_hooks,
            task=task,
        ):
            self._pending_notes[(project_id, task.id)] = WORKTREE_RECREATED_NOTE
        return TaskFiles(
            root=task.worktree,
            git_dir=git_dir_for_task(repo_root, task.id),
            empty_hooks=self._paths.empty_hooks,
            runner=self._git,
        )

    async def _task_env(self, project_id: str, task_id: str) -> tuple[Path, AsyncEngine]:
        project = await self._workspace.get(project_id)
        repo_root = self._workspace.repo_root(project)
        engine = await self._databases.project(project_id)
        return git_dir_for_task(repo_root, task_id), engine

    async def repo_ref(self, project_id: str) -> RepoRef | None:
        project = await self._workspace.get(project_id)
        return await resolve_repo_ref(
            self._git,
            project=project,
            repo_root=self._workspace.repo_root(project),
            empty_hooks=self._paths.empty_hooks,
        )

    async def has_commits_beyond_base(self, project_id: str, task: TaskRef) -> bool:
        async with self._task_lock(project_id, task.id):
            git_dir, engine = await self._task_env(project_id, task.id)
            return await has_commits_beyond_base_task(
                runner=self._git,
                git_dir=git_dir,
                worktree=task.worktree,
                empty_hooks=self._paths.empty_hooks,
                engine=engine,
                task=task,
            )

    async def has_unpublished_changes(self, project_id: str, task: TaskRef) -> bool:
        async with self._task_lock(project_id, task.id):
            git_dir, engine = await self._task_env(project_id, task.id)
            return await has_unpublished_changes_task(
                runner=self._git,
                git_dir=git_dir,
                worktree=task.worktree,
                empty_hooks=self._paths.empty_hooks,
                engine=engine,
                remote=await self.repo_ref(project_id),
                task=task,
            )

    async def staged_diff(self, project_id: str, task: TaskRef) -> str:
        async with self._task_lock(project_id, task.id):
            git_dir, engine = await self._task_env(project_id, task.id)
            async with pause_task(self._sandbox, project_id, task.id):
                return await staged_diff_task(
                    runner=self._git,
                    git_dir=git_dir,
                    worktree=task.worktree,
                    empty_hooks=self._paths.empty_hooks,
                    engine=engine,
                    task=task,
                )

    async def commit(self, project_id: str, task: TaskRef, message: str) -> CommitResult:
        async with self._task_lock(project_id, task.id):
            git_dir, engine = await self._task_env(project_id, task.id)
            async with pause_task(self._sandbox, project_id, task.id):
                return await commit_task(
                    runner=self._git,
                    git_dir=git_dir,
                    worktree=task.worktree,
                    empty_hooks=self._paths.empty_hooks,
                    databases=self._databases,
                    engine=engine,
                    task=task,
                    message=message,
                )

    async def push(self, project_id: str, task: TaskRef) -> None:
        async with self._task_lock(project_id, task.id):
            git_dir, _ = await self._task_env(project_id, task.id)
            await push_task(
                runner=self._git,
                git_dir=git_dir,
                worktree=task.worktree,
                empty_hooks=self._paths.empty_hooks,
                remote=await self.repo_ref(project_id),
                task=task,
            )

    async def local_merge_preview(self, project_id: str, task: TaskRef) -> list[str]:
        p = await self._workspace.get(project_id)
        return await local_merge_preview(
            self._git,
            self._workspace.repo_root(p),
            self._paths.empty_hooks,
            p.default_branch or "main",
            task.branch,
        )

    async def local_merge(self, project_id: str, task: TaskRef) -> None:
        p = await self._workspace.get(project_id)
        await local_merge(
            databases=self._databases,
            runner=self._git,
            repo_root=self._workspace.repo_root(p),
            empty_hooks=self._paths.empty_hooks,
            temp_base_dir=self._paths.project_dir(project_id) / "temp_merges",
            default_branch=p.default_branch or "main",
            task_branch=task.branch,
            task_id=task.id,
        )

    async def set_review(
        self,
        project_id: str,
        task_id: str,
        *,
        kind: Literal["github", "local"],
        pr_number: int | None = None,
        pr_url: str | None = None,
    ) -> TaskView:
        return await set_review_task(
            engine=await self._databases.project(project_id),
            project_id=project_id,
            task_id=task_id,
            kind=kind,
            pr_number=pr_number,
            pr_url=pr_url,
        )

    async def end(
        self, project_id: str, task_id: str, status: Literal["merged", "closed", "discarded"]
    ) -> None:
        async with self._task_lock(project_id, task_id):
            await self._end_locked(project_id, task_id, status)

    async def decide_review(
        self,
        project_id: str,
        task_id: str,
        step: Callable[[TaskRef], Awaitable[None]],
        status: Literal["merged", "closed"],
    ) -> TaskView:
        async with self._task_lock(project_id, task_id):
            task = await self.view(project_id, task_id)
            if task.status != "in_review" or task.review_kind != "local":
                raise DomainError(
                    "not_in_review", f"Task '{task_id}' is not awaiting local review.", status=409
                )
            # Only the id, title and branch matter to a step; the worktree is not touched.
            await step(
                TaskRef(
                    id=task.id,
                    title=task.title,
                    branch=task.branch,
                    worktree=Path(),
                    status=task.status,
                )
            )
            await self._end_locked(project_id, task_id, status)
            return await self.view(project_id, task_id)

    async def _end_locked(
        self, project_id: str, task_id: str, status: Literal["merged", "closed", "discarded"]
    ) -> None:
        p = await self._workspace.get(project_id)
        await end_task(
            runner=self._git,
            repo_root=self._workspace.repo_root(p),
            empty_hooks=self._paths.empty_hooks,
            databases=self._databases,
            project_id=project_id,
            task_id=task_id,
            status=status,
            sandbox=self._sandbox,
            events=self._events,
        )
        for (pid, sid), tid in list(self._current_tasks.items()):
            if pid == project_id and tid == task_id:
                self._current_tasks.pop((pid, sid), None)
        self._pending_notes.pop((project_id, task_id), None)
