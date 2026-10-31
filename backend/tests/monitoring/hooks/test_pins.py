"""Tests for GitHookPinner and PinCheckObserver (Task 7, #492)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from mlops_agents.agents.pr_sync import PrChange
from mlops_agents.integrations.github.git import GitError, GitResult, GitRunner
from mlops_agents.monitoring.hooks.contract import PinnedFile
from mlops_agents.monitoring.hooks.pins import (
    GitHookPinner,
    PinCheckObserver,
    hook_paths,
    verify_hook_pins,
)
from mlops_agents.monitoring.hooks.service import HooksService
from mlops_agents.proposals.handoffs import SqliteHandoffs
from mlops_agents.proposals.payloads import CommandSpec
from mlops_agents.workspace.models import Project
from mlops_agents.workspace.service import WorkspaceService
from tests.conftest import make_repo
from tests.proposals.samples import command


class FakeWorkspace:
    def __init__(self, project: Project, root: Path) -> None:
        self._project = project
        self._root = root

    async def get(self, project_id: str) -> Project:
        return self._project

    def repo_root(self, project: Project) -> Path:
        return self._root


def make_workspace(project: Project, root: Path) -> WorkspaceService:
    return cast(WorkspaceService, FakeWorkspace(project, root))


def _make_spec(
    workflow: str | None = None, entry_files: list[str] | None = None, deploys: bool = False
) -> CommandSpec:
    entries = entry_files if entry_files is not None else ["train.py"]
    if workflow:
        return CommandSpec.model_validate(
            command(
                kind="train",
                type="ci_dispatch",
                workflow=workflow,
                template=None,
                entry_files=entries,
                deploys=deploys,
            )
        )
    return CommandSpec.model_validate(
        command(
            kind="train",
            type="command",
            template="python train.py",
            entry_files=entries,
            deploys=deploys,
        )
    )


def test_hook_paths_deduplication_and_order() -> None:
    spec = _make_spec(
        workflow=".github/workflows/train.yml", entry_files=["train.py", "train.py", "util.py"]
    )
    paths = hook_paths(spec)
    assert paths == [".github/workflows/train.yml", "train.py", "util.py"]


@pytest.mark.asyncio
async def test_unchanged_file_passes(
    tmp_path: Path,
    git_env: dict[str, str],
    service: HooksService,
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"train.py": "print('hello')\n"}, git_env)
    git = GitRunner(tmp_path / "bin")
    project = Project(
        id="prj_1",
        name="p1",
        source="local",
        local_path=str(repo_dir),
        github_full_name=None,
        default_branch="main",
        status="ok",
        status_detail=None,
        created_at="2026-10-31T00:00:00Z",
        last_opened_at=None,
    )
    workspace = make_workspace(project, repo_dir)
    pinner = GitHookPinner(workspace, git)
    service.set_pinner(pinner)

    spec = _make_spec(entry_files=["train.py"])
    hook = await service.register(project.id, spec, "train", "churn", "handoff")

    assert hook.approved_commit is not None
    assert len(hook.pins) == 1
    assert hook.pins[0].path == "train.py"

    check = await verify_hook_pins(service, workspace, git, project.id, hook.id)
    assert check.ok is True
    assert check.changed == []

    observer = PinCheckObserver(service, workspace, git)
    await observer.synced(project.id)

    updated = await service.get(project.id, hook.id)
    assert updated is not None
    assert updated.status == "active"
    assert updated.suspended_reason is None


@pytest.mark.asyncio
async def test_spec_unpinned_file_suspended_on_next_pass(
    tmp_path: Path,
    git_env: dict[str, str],
    service: HooksService,
) -> None:
    repo_dir = make_repo(
        tmp_path / "repo",
        {"train.py": "print('hello')\n", "eval.py": "print('eval')\n"},
        git_env,
    )
    git = GitRunner(tmp_path / "bin")
    project = Project(
        id="prj_2",
        name="p2",
        source="local",
        local_path=str(repo_dir),
        github_full_name=None,
        default_branch="main",
        status="ok",
        status_detail=None,
        created_at="2026-10-31T00:00:00Z",
        last_opened_at=None,
    )
    workspace = make_workspace(project, repo_dir)
    pinner = GitHookPinner(workspace, git)
    service.set_pinner(pinner)

    # Initially pinned only to train.py
    spec = _make_spec(entry_files=["train.py"])
    hook = await service.register(project.id, spec, "train", "churn", "handoff")
    assert hook.status == "active"

    # Hook spec is updated to also include eval.py, but not re-pinned yet (head unchanged)
    new_spec = _make_spec(entry_files=["train.py", "eval.py"])
    await service._store.update_hook_spec(  # pyright: ignore[reportPrivateUsage]
        project.id,
        hook_id=hook.id,
        spec=new_spec.model_dump_json(),
    )

    observer = PinCheckObserver(service, workspace, git)
    await observer.synced(project.id)

    updated = await service.get(project.id, hook.id)
    assert updated is not None
    assert updated.status == "suspended"
    assert updated.suspended_reason == "Unpinned paths in hook spec: eval.py"


@pytest.mark.asyncio
async def test_hook_registered_before_pinner_existed_is_pinned_at_start_and_never_suspended(
    tmp_path: Path,
    git_env: dict[str, str],
    service: HooksService,
    handoffs: SqliteHandoffs,
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"train.py": "print('initial')\n"}, git_env)
    git = GitRunner(tmp_path / "bin")
    project = Project(
        id="prj_3",
        name="p3",
        source="local",
        local_path=str(repo_dir),
        github_full_name=None,
        default_branch="main",
        status="ok",
        status_detail=None,
        created_at="2026-10-31T00:00:00Z",
        last_opened_at=None,
    )
    workspace = make_workspace(project, repo_dir)

    # Register hook before pinner exists
    spec = _make_spec(entry_files=["train.py"])
    hook_unpinned = await service.register(project.id, spec, "train", "churn", "handoff")
    assert hook_unpinned.pins == []
    assert hook_unpinned.approved_commit is None

    # Now set pinner (e.g. at next startup)
    pinner = GitHookPinner(workspace, git)
    service.set_pinner(pinner)

    # Reconcile / re-register pins it
    hook_pinned = await service.register(project.id, spec, "train", "churn", "handoff")
    assert hook_pinned.approved_commit is not None
    assert len(hook_pinned.pins) == 1
    assert hook_pinned.pins[0].path == "train.py"

    # Sync observer does not suspend it
    observer = PinCheckObserver(service, workspace, git)
    await observer.synced(project.id)

    updated = await service.get(project.id, hook_pinned.id)
    assert updated is not None
    assert updated.status == "active"
    assert updated.suspended_reason is None


@pytest.mark.asyncio
async def test_changed_pinned_file_suspends_hook(
    tmp_path: Path,
    git_env: dict[str, str],
    service: HooksService,
) -> None:
    repo_dir = make_repo(tmp_path / "repo", {"train.py": "print('v1')\r\n"}, git_env)
    git = GitRunner(tmp_path / "bin")
    # Set autocrlf in repo to test CRLF consistency
    await git.run(["config", "core.autocrlf", "true"], cwd=repo_dir)

    project = Project(
        id="prj_4",
        name="p4",
        source="local",
        local_path=str(repo_dir),
        github_full_name=None,
        default_branch="main",
        status="ok",
        status_detail=None,
        created_at="2026-10-31T00:00:00Z",
        last_opened_at=None,
    )
    workspace = make_workspace(project, repo_dir)
    pinner = GitHookPinner(workspace, git)
    service.set_pinner(pinner)

    spec = _make_spec(entry_files=["train.py"])
    hook = await service.register(project.id, spec, "train", "churn", "handoff")
    assert hook.status == "active"

    # Run observer: head unchanged, passes
    observer = PinCheckObserver(service, workspace, git)
    await observer.synced(project.id)
    assert (await service.get(project.id, hook.id)).status == "active"  # type: ignore

    # Now modify train.py and commit to main
    (repo_dir / "train.py").write_text("print('v2_tampered')\r\n", encoding="utf-8")
    await git.run(["add", "train.py"], cwd=repo_dir, extra_env=git_env)
    await git.run(["commit", "-m", "update train.py"], cwd=repo_dir, extra_env=git_env)

    # Next sync pass: head moved, hashes file and discovers change -> suspends
    await observer.synced(project.id)

    suspended = await service.get(project.id, hook.id)
    assert suspended is not None
    assert suspended.status == "suspended"
    assert "Pinned files changed on default branch: train.py" in str(suspended.suspended_reason)


@pytest.mark.asyncio
async def test_merged_pr_touching_pinned_path_triggers_check(
    tmp_path: Path,
    git_env: dict[str, str],
    service: HooksService,
) -> None:
    repo_dir = make_repo(
        tmp_path / "repo", {"train.py": "print('v1')\n", "readme.md": "docs\n"}, git_env
    )
    git = GitRunner(tmp_path / "bin")
    project = Project(
        id="prj_5",
        name="p5",
        source="local",
        local_path=str(repo_dir),
        github_full_name=None,
        default_branch="main",
        status="ok",
        status_detail=None,
        created_at="2026-10-31T00:00:00Z",
        last_opened_at=None,
    )
    workspace = make_workspace(project, repo_dir)
    pinner = GitHookPinner(workspace, git)
    service.set_pinner(pinner)

    spec = _make_spec(entry_files=["train.py"])
    hook = await service.register(project.id, spec, "train", "churn", "handoff")

    observer = PinCheckObserver(service, workspace, git)

    # PR 1: merged PR only touching readme.md (not pinned) -> does not suspend
    pr_doc = PrChange(
        number=1,
        state="merged",
        merged_at=datetime.now(UTC),
        head_branch="patch-1",
        agent="developer",
        files=["readme.md"],
    )
    await observer.changed(project.id, pr_doc)
    assert (await service.get(project.id, hook.id)).status == "active"  # type: ignore

    # Now simulate a commit to train.py on main
    (repo_dir / "train.py").write_text("print('v2')\n", encoding="utf-8")
    await git.run(["add", "train.py"], cwd=repo_dir, extra_env=git_env)
    await git.run(["commit", "-m", "modify train"], cwd=repo_dir, extra_env=git_env)

    # PR 2: merged PR touching train.py -> triggers check and suspends
    pr_train = PrChange(
        number=2,
        state="merged",
        merged_at=datetime.now(UTC),
        head_branch="patch-2",
        agent="developer",
        files=["train.py"],
    )
    await observer.changed(project.id, pr_train)

    suspended = await service.get(project.id, hook.id)
    assert suspended is not None
    assert suspended.status == "suspended"
    assert "Pinned files changed on default branch: train.py" in str(suspended.suspended_reason)


@pytest.mark.asyncio
async def test_failed_fetch_suspends_nothing(
    tmp_path: Path,
    service: HooksService,
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    class FailingFetchGitRunner(GitRunner):
        def __init__(self) -> None:
            super().__init__(tmp_path / "bin")

        async def run(
            self,
            args: Sequence[str],
            cwd: Path | None = None,
            *,
            check: bool = True,
            timeout: float = 600,  # noqa: ASYNC109
            authenticated: bool = False,
            extra_env: dict[str, str] | None = None,
        ) -> GitResult:
            if "fetch" in args:
                raise GitError("Network unreachable", code=128)
            return GitResult(0, "abc1234\n", "")

    git = FailingFetchGitRunner()
    project = Project(
        id="prj_6",
        name="p6",
        source="github",
        local_path=None,
        github_full_name="octo/prj-6",
        default_branch="main",
        status="ok",
        status_detail=None,
        created_at="2026-10-31T00:00:00Z",
        last_opened_at=None,
    )
    workspace = make_workspace(project, repo_dir)

    spec = _make_spec(entry_files=["train.py"])
    # Register with existing pins manually
    hook = await service.register(project.id, spec, "train", "churn", "handoff")
    await service._store.update_hook_spec(  # pyright: ignore[reportPrivateUsage]
        project.id,
        hook_id=hook.id,
        spec=spec.model_dump_json(),
        approved_commit="abc1234",
        pins=[PinnedFile(path="train.py", sha256="dummy_sha")],
    )

    observer = PinCheckObserver(service, workspace, git)
    # Failed fetch skips check and does NOT suspend hook
    await observer.synced(project.id)

    updated = await service.get(project.id, hook.id)
    assert updated is not None
    assert updated.status == "active"
    assert updated.suspended_reason is None
