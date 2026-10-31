"""Git-backed HookPinner and PinCheckObserver for tracking hook integrity (Task 7, #492)."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from mlops_agents.agents.pr_sync import PrChange, PrObserver
from mlops_agents.monitoring.hooks.contract import (
    HookPinner,
    Hooks,
    HookView,
    PinCheck,
    PinnedFile,
)
from mlops_agents.monitoring.scheduler.contract import SyncObserver
from mlops_agents.proposals.payloads import CommandSpec

if TYPE_CHECKING:
    from mlops_agents.integrations.github.git import GitRunner
    from mlops_agents.workspace.service import WorkspaceService

logger = logging.getLogger(__name__)


def hook_paths(spec: CommandSpec) -> list[str]:
    """Returns the workflow file (if spec.workflow) plus declared entry_files (deduplicated, order preserved)."""
    paths: list[str] = []
    if spec.workflow:
        paths.append(spec.workflow)
    for path in spec.entry_files:
        if path not in paths:
            paths.append(path)
    return paths


class GitHookPinner(HookPinner):
    """Pins hook workflow and entry files by hashing git blob bytes at the default branch head."""

    def __init__(self, workspace: WorkspaceService, git: GitRunner) -> None:
        self._workspace = workspace
        self._git = git

    async def pins(self, project_id: str, hook: HookView) -> tuple[str, list[PinnedFile]]:
        try:
            project = await self._workspace.get(project_id)
        except Exception as exc:
            logger.warning("Failed to get project %s for pinning: %s", project_id, exc)
            return "", []

        repo_root = self._workspace.repo_root(project)
        default_branch = project.default_branch or "main"
        if project.source == "github":
            try:
                await self._git.run(
                    ["fetch", "origin", default_branch],
                    cwd=repo_root,
                    authenticated=True,
                )
                ref = f"origin/{default_branch}"
            except Exception as exc:
                logger.warning(
                    "Failed to fetch origin/%s for project %s: %s", default_branch, project_id, exc
                )
                return "", []
        else:
            ref = f"refs/heads/{default_branch}"

        rev = await self._git.run(["rev-parse", ref], cwd=repo_root, check=False)
        if rev.code != 0:
            logger.warning("Failed to rev-parse %s for project %s: %s", ref, project_id, rev.stderr)
            return "", []
        commit = rev.stdout.strip()

        pinned_files: list[PinnedFile] = []
        for path in hook_paths(hook.spec):
            res = await self._git.run(
                ["cat-file", "blob", f"{commit}:{path}"],
                cwd=repo_root,
                check=False,
            )
            if res.code == 0:
                sha = hashlib.sha256(res.stdout.encode("utf-8")).hexdigest()
                pinned_files.append(PinnedFile(path=path, sha256=sha))

        return commit, pinned_files


async def verify_hook_pins(
    hooks: Hooks,
    workspace: WorkspaceService,
    git: GitRunner,
    project_id: str,
    hook_id: str,
) -> PinCheck:
    """Verifies a hook's pinned files against the current default branch head."""
    hook = await hooks.get(project_id, hook_id)
    if hook is None or not hook.pins:
        return PinCheck(ok=True, changed=[])
    project = await workspace.get(project_id)
    repo_root = workspace.repo_root(project)
    default_branch = project.default_branch or "main"
    if project.source == "github":
        try:
            await git.run(
                ["fetch", "origin", default_branch],
                cwd=repo_root,
                authenticated=True,
            )
            ref = f"origin/{default_branch}"
        except Exception as exc:
            logger.warning(
                "Failed to fetch origin/%s for project %s: %s",
                default_branch,
                project_id,
                exc,
            )
            return PinCheck(ok=True, changed=[])
    else:
        ref = f"refs/heads/{default_branch}"

    res = await git.run(["rev-parse", ref], cwd=repo_root, check=False)
    if res.code != 0:
        return PinCheck(ok=True, changed=[])
    commit = res.stdout.strip()
    changed: list[str] = []
    for pin in hook.pins:
        blob_res = await git.run(
            ["cat-file", "blob", f"{commit}:{pin.path}"],
            cwd=repo_root,
            check=False,
        )
        if blob_res.code != 0:
            changed.append(pin.path)
        else:
            sha = hashlib.sha256(blob_res.stdout.encode("utf-8")).hexdigest()
            if sha != pin.sha256:
                changed.append(pin.path)
    return PinCheck(ok=len(changed) == 0, changed=changed)


class PinCheckObserver(SyncObserver, PrObserver):
    """Observer that checks hook pin integrity on every sync pass and on merged PRs."""

    def __init__(
        self,
        hooks: Hooks,
        workspace: WorkspaceService,
        git: GitRunner,
    ) -> None:
        self._hooks = hooks
        self._workspace = workspace
        self._git = git
        self._last_checked_head: dict[str, str] = {}

    async def synced(self, project_id: str, now: datetime | None = None) -> None:
        # 1. Spec against pins (every pass, head moved or not)
        hooks = await self._hooks.list(project_id)
        for hook in hooks:
            if hook.approved_commit is not None and hook.status != "suspended":
                required = set(hook_paths(hook.spec))
                pinned = {p.path for p in hook.pins}
                missing = required - pinned
                if missing:
                    await self._hooks.suspend(
                        project_id,
                        hook.id,
                        f"Unpinned paths in hook spec: {', '.join(sorted(missing))}",
                    )

        # 2. Head moved check
        project = await self._workspace.get(project_id)
        repo_root = self._workspace.repo_root(project)
        default_branch = project.default_branch or "main"
        if project.source == "github":
            try:
                await self._git.run(
                    ["fetch", "origin", default_branch],
                    cwd=repo_root,
                    authenticated=True,
                )
                ref = f"origin/{default_branch}"
            except Exception as exc:
                logger.warning(
                    "Failed to fetch origin/%s for project %s: %s",
                    default_branch,
                    project_id,
                    exc,
                )
                return
        else:
            ref = f"refs/heads/{default_branch}"

        res = await self._git.run(["rev-parse", ref], cwd=repo_root, check=False)
        if res.code != 0:
            logger.warning(
                "Failed to rev-parse %s for project %s: %s",
                ref,
                project_id,
                res.stderr,
            )
            return
        head_commit = res.stdout.strip()
        last_commit = self._last_checked_head.get(project_id)

        # Files are hashed only when default branch head moved since last check
        if last_commit != head_commit:
            approved_hooks = [
                h
                for h in await self._hooks.list(project_id)
                if h.approved_commit is not None and h.status != "suspended"
            ]
            for hook in approved_hooks:
                changed_paths = await self._check_hook_pins(repo_root, head_commit, hook)
                if changed_paths:
                    await self._hooks.suspend(
                        project_id,
                        hook.id,
                        f"Pinned files changed on default branch: {', '.join(sorted(changed_paths))}",
                    )
            self._last_checked_head[project_id] = head_commit

    async def changed(self, project_id: str, pr: PrChange) -> None:
        if pr.state != "merged" and pr.merged_at is None:
            return
        hooks = await self._hooks.list(project_id)
        approved_hooks = [
            h for h in hooks if h.approved_commit is not None and h.status != "suspended"
        ]
        pr_files_set = set(pr.files)
        hooks_to_check = [h for h in approved_hooks if any(p.path in pr_files_set for p in h.pins)]
        if not hooks_to_check:
            return

        project = await self._workspace.get(project_id)
        repo_root = self._workspace.repo_root(project)
        default_branch = project.default_branch or "main"
        if project.source == "github":
            try:
                await self._git.run(
                    ["fetch", "origin", default_branch],
                    cwd=repo_root,
                    authenticated=True,
                )
                ref = f"origin/{default_branch}"
            except Exception as exc:
                logger.warning(
                    "Failed to fetch origin/%s for project %s: %s",
                    default_branch,
                    project_id,
                    exc,
                )
                return
        else:
            ref = f"refs/heads/{default_branch}"

        res = await self._git.run(["rev-parse", ref], cwd=repo_root, check=False)
        if res.code != 0:
            return
        head_commit = res.stdout.strip()

        for hook in hooks_to_check:
            changed_paths = await self._check_hook_pins(repo_root, head_commit, hook)
            if changed_paths:
                await self._hooks.suspend(
                    project_id,
                    hook.id,
                    f"Pinned files changed on default branch: {', '.join(sorted(changed_paths))}",
                )
        self._last_checked_head[project_id] = head_commit

    async def _check_hook_pins(
        self, repo_root: Path, head_commit: str, hook: HookView
    ) -> list[str]:
        changed: list[str] = []
        for pin in hook.pins:
            res = await self._git.run(
                ["cat-file", "blob", f"{head_commit}:{pin.path}"],
                cwd=repo_root,
                check=False,
            )
            if res.code != 0:
                changed.append(pin.path)
            else:
                sha = hashlib.sha256(res.stdout.encode("utf-8")).hexdigest()
                if sha != pin.sha256:
                    changed.append(pin.path)
        return changed
