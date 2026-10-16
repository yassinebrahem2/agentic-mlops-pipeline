from __future__ import annotations

import asyncio
import os
import shutil
import stat
import string
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import RowMapping, delete, insert, select, update

from mlops_agents.clock import now_iso
from mlops_agents.config import RunMode, Settings
from mlops_agents.errors import DomainError
from mlops_agents.ids import new_id
from mlops_agents.integrations.github.api import get_login, get_repo
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.git import GitError, GitRunner
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import projects
from mlops_agents.workspace.models import FolderEntry, FolderListing, Project, ProjectStatus
from mlops_agents.workspace.refs import parse_github_ref, path_key

EMPTY_REPO_DETAIL = "The repository has no committed files yet. Commit your files first."
MISSING_DETAIL = "The folder no longer exists. Point the project to its new location."
NOT_A_REPO_DETAIL = "The folder is no longer a git repository."


def github_https_url(full_name: str) -> str:
    return f"https://github.com/{full_name}.git"


def _rmtree(path: Path) -> None:
    def make_writable_and_retry(func: Callable[..., Any], target: str, exc: BaseException) -> None:
        del exc
        Path(target).chmod(
            stat.S_IWRITE
        )  # git marks pack files read-only; Windows refuses to delete them
        func(target)

    if path.exists():
        shutil.rmtree(path, onexc=make_writable_and_retry)


def _user_path(text: str) -> Path:
    return Path(text.strip()).expanduser()


def _drive_roots() -> list[str]:
    if os.name != "nt":
        return ["/"]
    # Probing a disconnected network drive can take seconds; this runs in a worker thread.
    return [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]


class WorkspaceService:
    def __init__(
        self,
        settings: Settings,
        paths: DataPaths,
        databases: Databases,
        git: GitRunner,
        github: GitHubAuth,
        http: httpx.AsyncClient,
        *,
        remote_url: Callable[[str], str] = github_https_url,
    ) -> None:
        self._settings = settings
        self._paths = paths
        self._dbs = databases
        self._git = git
        self._github = github
        self._http = http
        self._remote_url = remote_url

    # Queries

    async def list_projects(self) -> list[Project]:
        engine = await self._dbs.app()
        query = select(projects).order_by(
            projects.c.last_opened_at.is_(None),
            projects.c.last_opened_at.desc(),
            projects.c.created_at.desc(),
        )
        async with engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [_project(row) for row in rows]

    async def get(self, project_id: str) -> Project:
        engine = await self._dbs.app()
        async with engine.connect() as conn:
            row = (
                (await conn.execute(select(projects).where(projects.c.id == project_id)))
                .mappings()
                .first()
            )
        if row is None:
            raise DomainError("project_not_found", "This project does not exist.", status=404)
        return _project(row)

    def repo_root(self, project: Project) -> Path:
        if project.source == "local" and project.local_path is not None:
            return Path(project.local_path)
        return self._paths.repo_dir(project.id)

    # Local projects

    def _require_local_mode(self) -> None:
        if self._settings.mode is RunMode.SERVER:
            raise DomainError(
                "local_projects_disabled",
                "This server only works with GitHub repositories.",
                status=403,
            )

    async def _repo_folder(self, path_text: str, *, init_git: bool) -> tuple[Path, str]:
        folder = await asyncio.to_thread(_user_path, path_text)
        if not await asyncio.to_thread(folder.is_dir):
            raise DomainError(
                "folder_not_found", f"{folder} is not a folder on this machine.", status=422
            )
        key = await asyncio.to_thread(path_key, folder)
        top = await self._git.run(["rev-parse", "--show-toplevel"], cwd=folder, check=False)
        if top.code != 0:
            if not init_git:
                raise DomainError(
                    "not_a_git_repo",
                    f"{folder} is not a git repository. The app can run git init in it for you.",
                    status=422,
                    detail={"can_init": True},
                )
            await self._git.run(["init"], cwd=folder)
        elif await asyncio.to_thread(path_key, Path(top.stdout.strip())) != key:
            root = top.stdout.strip()
            raise DomainError(
                "not_repo_root",
                f"This folder is inside the repository {root}. Add that folder instead.",
                status=422,
                detail={"repo_root": root},
            )
        return await asyncio.to_thread(folder.resolve), key

    async def add_local(self, path_text: str, *, init_git: bool = False) -> Project:
        self._require_local_mode()
        folder, key = await self._repo_folder(path_text, init_git=init_git)
        await self._ensure_unique(local_path_key=key)
        status, detail = await self._status(folder)
        project_id = new_id("prj")
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            await conn.execute(
                insert(projects).values(
                    id=project_id,
                    name=folder.name,
                    source="local",
                    local_path=str(folder),
                    local_path_key=key,
                    default_branch=await self._current_branch(folder),
                    status=status,
                    status_detail=detail,
                    created_at=now_iso(),
                )
            )
        return await self.get(project_id)

    async def relink(self, project_id: str, path_text: str) -> Project:
        self._require_local_mode()
        project = await self.get(project_id)
        if project.source != "local":
            raise DomainError(
                "not_local", "Only local projects can be pointed to a new folder.", status=409
            )
        folder, key = await self._repo_folder(path_text, init_git=False)
        await self._ensure_unique(local_path_key=key, except_id=project_id)
        status, detail = await self._status(folder)
        await self._update(
            project_id,
            local_path=str(folder),
            local_path_key=key,
            default_branch=await self._current_branch(folder),
            status=status,
            status_detail=detail,
        )
        return await self.get(project_id)

    async def browse(self, path_text: str | None) -> FolderListing:
        self._require_local_mode()
        folder = await asyncio.to_thread(_user_path, path_text) if path_text else Path.home()
        if not await asyncio.to_thread(folder.is_dir):
            raise DomainError(
                "folder_not_found", f"{folder} is not a folder on this machine.", status=404
            )

        def scan() -> list[FolderEntry]:
            found: list[FolderEntry] = []
            for child in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
                try:
                    if child.is_dir():
                        found.append(
                            FolderEntry(
                                name=child.name,
                                path=str(child),
                                is_git_repo=(child / ".git").exists(),
                            )
                        )
                except OSError:
                    continue
            return found

        try:
            entries = await asyncio.to_thread(scan)
        except PermissionError:
            raise DomainError(
                "folder_unreadable", f"The app is not allowed to read {folder}.", status=403
            ) from None
        roots = await asyncio.to_thread(_drive_roots)
        parent = str(folder.parent) if folder.parent != folder else None
        return FolderListing(path=str(folder), parent=parent, entries=entries, roots=roots)

    # GitHub projects

    async def _clone(self, project_id: str, full_name: str) -> tuple[ProjectStatus, str | None]:
        target = self._paths.repo_dir(project_id)
        await asyncio.to_thread(_rmtree, target)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        try:
            result = await self._git.run(
                ["clone", "--origin", "origin", self._remote_url(full_name), str(target)],
                check=False,
                authenticated=True,
            )
        except DomainError as err:  # no GitHub token: gh logged out, or GitHub disconnected
            reason = err.message.rstrip(".")
            return "clone_failed", f"Cloning failed: {reason}. Use Refresh to try again."
        if result.code != 0:
            reason = (await self._failure_reason(result.stderr)).rstrip(".")
            return "clone_failed", f"Cloning failed: {reason}. Use Refresh to try again."
        return await self._status(target)

    async def add_github(self, ref_text: str) -> Project:
        full_name = parse_github_ref(ref_text)
        token = await self._github.require_token()
        repo = await get_repo(self._http, token, full_name)
        key = repo.full_name.casefold()
        await self._ensure_unique(github_key=key)
        project_id = new_id("prj")
        status, detail = await self._clone(project_id, repo.full_name)
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            await conn.execute(
                insert(projects).values(
                    id=project_id,
                    name=repo.full_name.split("/", 1)[1],
                    source="github",
                    github_full_name=repo.full_name,
                    github_key=key,
                    default_branch=repo.default_branch,
                    status=status,
                    status_detail=detail,
                    created_at=now_iso(),
                )
            )
        return await self.get(project_id)

    async def _sync_clone(self, project: Project) -> None:
        """Fetch and reset the managed clone to the remote's default branch. Never edited by the
        app, so a hard reset is safe; it also survives force-pushes and renamed branches."""
        if project.github_full_name is None:
            return
        target = self._paths.repo_dir(project.id)
        if not await asyncio.to_thread((target / ".git").is_dir):
            status, detail = await self._clone(project.id, project.github_full_name)
            await self._update(project.id, status=status, status_detail=detail)
            return
        try:
            fetch = await self._git.run(
                ["fetch", "--prune", "origin"], cwd=target, check=False, authenticated=True
            )
        except DomainError as err:  # no GitHub token: gh logged out, or GitHub disconnected
            await self._refresh_failed(project.id, target, err.message)
            return
        if fetch.code != 0:
            await self._refresh_failed(project.id, target, await self._failure_reason(fetch.stderr))
            return
        try:
            await self._git.run(
                ["remote", "set-head", "origin", "--auto"],
                cwd=target,
                check=False,
                authenticated=True,
            )
            head = await self._git.run(
                ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=target, check=False
            )
            branch = head.stdout.strip().removeprefix("origin/") or project.default_branch or "main"
            await self._git.run(
                ["checkout", "--force", "-B", branch, f"origin/{branch}"], cwd=target
            )
            await self._git.run(["reset", "--hard", f"origin/{branch}"], cwd=target)
            await self._git.run(["clean", "-ffdx"], cwd=target)
        except (GitError, DomainError) as err:  # set-head reads the token again
            status, detail = await self._status(target)
            await self._update(
                project.id, status=status, status_detail=detail or f"Refresh failed: {err}"
            )
            return
        status, detail = await self._status(target)
        await self._update(project.id, default_branch=branch, status=status, status_detail=detail)

    async def _failure_reason(self, stderr: str) -> str:
        """Why a fetch or clone failed. git's own text is localized and names no fix, so a
        rejected token is detected by asking GitHub, whose message says what to do."""
        try:
            token = await self._github.token()
            if token is not None:
                await get_login(self._http, token)
        except DomainError as err:
            if err.code in ("github_token_rejected", "gh_not_logged_in", "github_token_unreadable"):
                return err.message
        return stderr.strip() or "no output"

    async def _refresh_failed(self, project_id: str, target: Path, reason: str) -> None:
        status, _ = await self._status(target)
        await self._update(
            project_id,
            status=status,
            status_detail=f"Refresh failed: {reason.rstrip('.')}. The last downloaded copy is still used.",
        )

    # Both kinds

    async def refresh(self, project_id: str) -> Project:
        project = await self.get(project_id)
        if project.source == "github":
            await self._sync_clone(project)
        else:
            root = self.repo_root(project)
            status, detail = await self._status(root)
            branch = (
                await self._current_branch(root) if status != "missing" else project.default_branch
            )
            await self._update(
                project_id, status=status, status_detail=detail, default_branch=branch
            )
        return await self.get(project_id)

    async def open(self, project_id: str) -> Project:
        await self.get(project_id)
        await self._update(project_id, last_opened_at=now_iso())
        return await self.refresh(project_id)

    async def remove(self, project_id: str) -> None:
        await self.get(project_id)
        await self._dbs.close_project(project_id)
        await asyncio.to_thread(_rmtree, self._paths.project_dir(project_id))
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            await conn.execute(delete(projects).where(projects.c.id == project_id))

    # Helpers

    async def _status(self, root: Path) -> tuple[ProjectStatus, str | None]:
        if not await asyncio.to_thread(root.is_dir):
            return "missing", MISSING_DETAIL
        top = await self._git.run(["rev-parse", "--show-toplevel"], cwd=root, check=False)
        if top.code != 0:
            return "missing", NOT_A_REPO_DETAIL
        tracked = await self._git.run(["ls-files", "-z"], cwd=root)
        if not tracked.stdout:
            return "empty", EMPTY_REPO_DETAIL
        return "ok", None

    async def _current_branch(self, root: Path) -> str | None:
        result = await self._git.run(["symbolic-ref", "--short", "HEAD"], cwd=root, check=False)
        return result.stdout.strip() or None

    async def _ensure_unique(
        self,
        *,
        local_path_key: str | None = None,
        github_key: str | None = None,
        except_id: str | None = None,
    ) -> None:
        condition = (
            projects.c.local_path_key == local_path_key
            if local_path_key is not None
            else projects.c.github_key == github_key
        )
        if except_id is not None:
            condition = condition & (projects.c.id != except_id)
        engine = await self._dbs.app()
        async with engine.connect() as conn:
            row = (
                (await conn.execute(select(projects.c.id, projects.c.name).where(condition)))
                .mappings()
                .first()
            )
        if row is not None:
            raise DomainError(
                "project_exists",
                f"This repository is already the project {row['name']}.",
                status=409,
                detail={"project_id": row["id"]},
            )

    async def _update(self, project_id: str, **values: object) -> None:
        engine = await self._dbs.app()
        async with engine.begin() as conn:
            await conn.execute(update(projects).where(projects.c.id == project_id).values(**values))


def _project(row: RowMapping) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        source=row["source"],
        local_path=row["local_path"],
        github_full_name=row["github_full_name"],
        default_branch=row["default_branch"],
        status=row["status"],
        status_detail=row["status_detail"],
        created_at=row["created_at"],
        last_opened_at=row["last_opened_at"],
    )
