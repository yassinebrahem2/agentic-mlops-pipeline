from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select
from sqlalchemy.engine import RowMapping

from mlops_agents.agents.runtime.contract import TaskView
from mlops_agents.agents.tasks.claims import claim_paths, get_task_read_hash, record_task_read
from mlops_agents.agents.tasks.commit import (
    commit_task,
    has_unpublished_changes_task,
    push_task,
    staged_diff_task,
)
from mlops_agents.agents.tasks.contract import CommitResult, Tasks
from mlops_agents.agents.tasks.git import git_dir_for_task
from mlops_agents.agents.tasks.lifecycle import (
    end_task,
    resume_task,
    set_review_task,
    start_task,
)
from mlops_agents.agents.tasks.local_merge import local_merge, local_merge_preview
from mlops_agents.agents.tasks.worktrees import (
    TaskFiles,
    clean_leftover_temp_files,
    recreate_worktree,
)
from mlops_agents.agents.tools.base import FileAccess, TaskRef
from mlops_agents.agents.tools.file_ops import pause_task
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog
from mlops_agents.ids import new_id
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import tasks
from mlops_agents.workspace.service import WorkspaceService

if TYPE_CHECKING:
    from mlops_agents.sandbox.contract import Sandbox


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

    def pop_pending_note(self, project_id: str, task_id: str) -> str | None:
        return self._pending_notes.pop((project_id, task_id), None)

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
            session_id=session_id,
            agent=agent,
            task_id=task_id,
        )
        if old_sid and old_sid != session_id:
            self._current_tasks.pop((project_id, old_sid), None)
        self._current_tasks[(project_id, session_id)] = task_id
        return ref

    async def _get_task_ref(self, project_id: str, row: RowMapping) -> TaskRef:
        worktree_path = Path(row["worktree"])
        if not await asyncio.to_thread(worktree_path.is_dir):
            p = await self._workspace.get(project_id)
            await recreate_worktree(
                runner=self._git,
                repo_root=self._workspace.repo_root(p),
                worktree_path=worktree_path,
                empty_hooks=self._paths.empty_hooks,
                task_id=row["id"],
                branch=row["branch"],
            )
            note = "[Note: Uncommitted changes were lost because the task worktree was recreated.]"
            self._pending_notes[(project_id, row["id"])] = note
        else:
            await asyncio.to_thread(clean_leftover_temp_files, worktree_path)
            note = self._pending_notes.get((project_id, row["id"]))

        return TaskRef(
            id=row["id"],
            title=row["title"],
            branch=row["branch"],
            worktree=worktree_path,
            status=row["status"],
            pending_note=note,
        )

    async def current(self, project_id: str, session_id: str) -> TaskRef | None:
        engine = await self._databases.project(project_id)
        task_id = self._current_tasks.get((project_id, session_id))
        if task_id:
            async with engine.connect() as conn:
                row = (
                    (await conn.execute(select(tasks).where(tasks.c.id == task_id)))
                    .mappings()
                    .first()
                )
            if (
                row is not None
                and row["status"] in ("active", "in_review")
                and row["session_id"] == session_id
            ):
                return await self._get_task_ref(project_id, row)
            self._current_tasks.pop((project_id, session_id), None)

        async with engine.connect() as conn:
            stmt = (
                select(tasks)
                .where(
                    tasks.c.session_id == session_id,
                    tasks.c.status.in_(("active", "in_review")),
                )
                .order_by(tasks.c.created_at.desc())
            )
            row = (await conn.execute(stmt)).mappings().first()

        if row is None:
            return None

        self._current_tasks[(project_id, session_id)] = row["id"]
        return await self._get_task_ref(project_id, row)

    def _build_task_view(self, row: RowMapping) -> TaskView:
        return TaskView(
            id=row["id"],
            agent=row["agent"],
            title=row["title"],
            branch=row["branch"],
            status=row["status"],
            review_kind=row["review_kind"],
            pr_number=row["pr_number"],
            pr_url=row["pr_url"],
        )

    async def view(self, project_id: str, task_id: str) -> TaskView:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().first()
            )

        if row is None:
            raise DomainError("task_not_found", f"Task {task_id} not found.", status=404)

        return self._build_task_view(row)

    async def record_read(self, project_id: str, task: TaskRef, path: str, sha256: str) -> None:
        await record_task_read(await self._databases.project(project_id), task.id, path, sha256)

    async def read_hash(self, project_id: str, task: TaskRef, path: str) -> str | None:
        return await get_task_read_hash(await self._databases.project(project_id), task.id, path)

    async def claim(self, project_id: str, task: TaskRef, paths: Sequence[str]) -> None:
        engine = await self._databases.project(project_id)
        await claim_paths(engine, task, paths)

    async def open_tasks(self, project_id: str) -> list[TaskView]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            stmt = (
                select(tasks)
                .where(tasks.c.status.in_(("active", "in_review")))
                .order_by(tasks.c.created_at.asc())
            )
            rows = (await conn.execute(stmt)).mappings().all()
        return [self._build_task_view(row) for row in rows]

    async def files(self, project_id: str, task: TaskRef | None) -> FileAccess:
        project = await self._workspace.get(project_id)
        repo_root = self._workspace.repo_root(project)
        if task is None:
            return RepoFiles(repo_root, self._git)
        return TaskFiles(
            root=task.worktree,
            git_dir=git_dir_for_task(repo_root, task.id),
            empty_hooks=self._paths.empty_hooks,
            runner=self._git,
        )

    async def _repo_and_git_dir(
        self, project_id: str, task_id: str
    ) -> tuple[WorkspaceService, Path, Path]:
        project = await self._workspace.get(project_id)
        repo_root = self._workspace.repo_root(project)
        return project, repo_root, git_dir_for_task(repo_root, task_id)  # type: ignore[return-value]

    async def has_unpublished_changes(self, project_id: str, task: TaskRef) -> bool:
        project, _, git_dir = await self._repo_and_git_dir(project_id, task.id)
        return await has_unpublished_changes_task(
            runner=self._git,
            git_dir=git_dir,
            worktree=task.worktree,
            empty_hooks=self._paths.empty_hooks,
            engine=await self._databases.project(project_id),
            project=project,  # type: ignore[arg-type]
            task=task,
        )

    async def staged_diff(self, project_id: str, task: TaskRef) -> str:
        _, _, git_dir = await self._repo_and_git_dir(project_id, task.id)
        async with pause_task(self._sandbox, project_id, task.id):
            return await staged_diff_task(
                runner=self._git,
                git_dir=git_dir,
                worktree=task.worktree,
                empty_hooks=self._paths.empty_hooks,
                engine=await self._databases.project(project_id),
                task=task,
            )

    async def commit(self, project_id: str, task: TaskRef, message: str) -> CommitResult:
        _, _, git_dir = await self._repo_and_git_dir(project_id, task.id)
        engine = await self._databases.project(project_id)
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
        project, _, git_dir = await self._repo_and_git_dir(project_id, task.id)
        await push_task(
            runner=self._git,
            git_dir=git_dir,
            worktree=task.worktree,
            empty_hooks=self._paths.empty_hooks,
            project=project,  # type: ignore[arg-type]
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
            self._git,
            self._workspace.repo_root(p),
            self._paths.empty_hooks,
            self._paths.project_dir(project_id) / "temp_merges",
            p.default_branch or "main",
            task.branch,
            task.id,
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
