from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import RowMapping

from mlops_agents.agents.runtime.contract import TaskView
from mlops_agents.agents.tasks.claims import claim_paths
from mlops_agents.agents.tasks.commit import (
    commit_task,
    has_unpublished_changes_task,
    push_task,
    staged_diff_task,
)
from mlops_agents.agents.tasks.contract import CommitResult, Tasks
from mlops_agents.agents.tasks.git import git_dir_for_task
from mlops_agents.agents.tasks.worktrees import TaskFiles, create_worktree
from mlops_agents.agents.tools.base import FileAccess, TaskRef
from mlops_agents.agents.tools.file_ops import pause_task
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.clock import now_iso
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog
from mlops_agents.ids import new_id
from mlops_agents.integrations.github.git import GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import task_reads, tasks
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
        repo_root = self._workspace.repo_root(project)
        worktree_path = self._paths.worktree_path(project_id, task_id)

        branch, base_ref, base_sha = await create_worktree(
            runner=self._git,
            project=project,
            repo_root=repo_root,
            worktree_path=worktree_path,
            empty_hooks=self._paths.empty_hooks,
            task_id=task_id,
            agent=agent,
            title=title,
        )

        engine = await self._databases.project(project_id)
        created_at = now_iso()
        async with engine.begin() as conn:
            await conn.execute(
                insert(tasks).values(
                    id=task_id,
                    session_id=session_id,
                    agent=agent,
                    title=title,
                    branch=branch,
                    worktree=str(worktree_path),
                    base_ref=base_ref,
                    base_sha=base_sha,
                    status="active",
                    review_kind=None,
                    pr_number=None,
                    pr_url=None,
                    created_at=created_at,
                    ended_at=None,
                )
            )

        self._current_tasks[(project_id, session_id)] = task_id

        await self._events.record(
            project_id,
            source=agent,
            type="task.started",
            payload={
                "task_id": task_id,
                "title": title,
                "branch": branch,
            },
            subject_id=task_id,
        )

        return TaskRef(
            id=task_id,
            title=title,
            branch=branch,
            worktree=worktree_path,
            status="active",
        )

    async def resume(
        self,
        project_id: str,
        session_id: str,
        agent: str,
        task_id: str,
    ) -> TaskRef:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().first()
            )

        if row is None:
            raise DomainError("task_not_found", f"Task {task_id} not found.", status=404)

        if row["agent"] != agent:
            raise DomainError(
                "task_agent_mismatch",
                f"Task {task_id} belongs to agent {row['agent']!r}, not {agent!r}.",
                status=403,
            )

        if row["status"] not in ("active", "in_review"):
            raise DomainError(
                "task_not_open",
                f"Task {task_id} has status {row['status']!r} and cannot be resumed.",
                status=409,
            )

        if row["session_id"] != session_id:
            old_session_id = str(row["session_id"])
            self._current_tasks.pop((project_id, old_session_id), None)
            async with engine.begin() as conn:
                await conn.execute(
                    update(tasks).where(tasks.c.id == task_id).values(session_id=session_id)
                )

        self._current_tasks[(project_id, session_id)] = task_id

        return TaskRef(
            id=row["id"],
            title=row["title"],
            branch=row["branch"],
            worktree=Path(row["worktree"]),
            status=row["status"],
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
                return TaskRef(
                    id=row["id"],
                    title=row["title"],
                    branch=row["branch"],
                    worktree=Path(row["worktree"]),
                    status=row["status"],
                )
            self._current_tasks.pop((project_id, session_id), None)

        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(tasks)
                        .where(
                            tasks.c.session_id == session_id,
                            tasks.c.status.in_(("active", "in_review")),
                        )
                        .order_by(tasks.c.created_at.desc())
                    )
                )
                .mappings()
                .first()
            )

        if row is None:
            return None

        self._current_tasks[(project_id, session_id)] = row["id"]
        return TaskRef(
            id=row["id"],
            title=row["title"],
            branch=row["branch"],
            worktree=Path(row["worktree"]),
            status=row["status"],
        )

    def _build_task_view(self, row: RowMapping) -> TaskView:
        kwargs: dict[str, Any] = {
            "id": row["id"],
            "title": row["title"],
            "branch": row["branch"],
            "status": row["status"],
            "review_kind": row["review_kind"],
            "pr_number": row["pr_number"],
            "pr_url": row["pr_url"],
        }
        if "agent" in TaskView.model_fields:
            kwargs["agent"] = row["agent"]
        return TaskView(**kwargs)

    async def view(self, project_id: str, task_id: str) -> TaskView:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            row = (
                (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().first()
            )

        if row is None:
            raise DomainError("task_not_found", f"Task {task_id} not found.", status=404)

        return self._build_task_view(row)

    async def open_tasks(self, project_id: str) -> list[TaskView]:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(tasks)
                        .where(tasks.c.status.in_(("active", "in_review")))
                        .order_by(tasks.c.created_at.asc())
                    )
                )
                .mappings()
                .all()
            )

        return [self._build_task_view(row) for row in rows]

    async def files(self, project_id: str, task: TaskRef | None) -> FileAccess:
        project = await self._workspace.get(project_id)
        repo_root = self._workspace.repo_root(project)
        if task is None:
            return RepoFiles(repo_root, self._git)

        git_dir = git_dir_for_task(repo_root, task.id)
        return TaskFiles(
            root=task.worktree,
            git_dir=git_dir,
            empty_hooks=self._paths.empty_hooks,
            runner=self._git,
        )

    # Future task methods (produced by tasks 2 to 5)
    async def record_read(self, project_id: str, task: TaskRef, path: str, sha256: str) -> None:
        engine = await self._databases.project(project_id)
        async with engine.begin() as conn:
            await conn.execute(
                delete(task_reads).where(
                    task_reads.c.task_id == task.id,
                    task_reads.c.path == path,
                )
            )
            await conn.execute(
                insert(task_reads).values(
                    task_id=task.id,
                    path=path,
                    sha256=sha256,
                )
            )

    async def read_hash(self, project_id: str, task: TaskRef, path: str) -> str | None:
        engine = await self._databases.project(project_id)
        async with engine.connect() as conn:
            res = await conn.execute(
                select(task_reads.c.sha256).where(
                    task_reads.c.task_id == task.id,
                    task_reads.c.path == path,
                )
            )
            return res.scalar_one_or_none()

    async def claim(self, project_id: str, task: TaskRef, paths: Sequence[str]) -> None:
        engine = await self._databases.project(project_id)
        await claim_paths(engine, task, paths)

    async def has_unpublished_changes(self, project_id: str, task: TaskRef) -> bool:
        project = await self._workspace.get(project_id)
        repo_root = self._workspace.repo_root(project)
        git_dir = git_dir_for_task(repo_root, task.id)
        engine = await self._databases.project(project_id)
        return await has_unpublished_changes_task(
            runner=self._git,
            git_dir=git_dir,
            worktree=task.worktree,
            empty_hooks=self._paths.empty_hooks,
            engine=engine,
            project=project,
            task=task,
        )

    async def staged_diff(self, project_id: str, task: TaskRef) -> str:
        project = await self._workspace.get(project_id)
        repo_root = self._workspace.repo_root(project)
        git_dir = git_dir_for_task(repo_root, task.id)
        engine = await self._databases.project(project_id)
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
        project = await self._workspace.get(project_id)
        repo_root = self._workspace.repo_root(project)
        git_dir = git_dir_for_task(repo_root, task.id)
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
        project = await self._workspace.get(project_id)
        repo_root = self._workspace.repo_root(project)
        git_dir = git_dir_for_task(repo_root, task.id)
        await push_task(
            runner=self._git,
            git_dir=git_dir,
            worktree=task.worktree,
            empty_hooks=self._paths.empty_hooks,
            project=project,
            task=task,
        )

    async def local_merge_preview(self, project_id: str, task: TaskRef) -> list[str]:
        raise NotImplementedError

    async def local_merge(self, project_id: str, task: TaskRef) -> None:
        raise NotImplementedError

    async def set_review(
        self,
        project_id: str,
        task_id: str,
        *,
        kind: Literal["github", "local"],
        pr_number: int | None = None,
        pr_url: str | None = None,
    ) -> TaskView:
        raise NotImplementedError

    async def end(
        self, project_id: str, task_id: str, status: Literal["merged", "closed", "discarded"]
    ) -> None:
        raise NotImplementedError
