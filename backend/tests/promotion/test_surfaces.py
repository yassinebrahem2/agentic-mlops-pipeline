from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import cast

import httpx2
import pytest

from mlops_agents.agents.tasks.contract import Tasks
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github import api
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.pulls import create_pull
from mlops_agents.monitoring.hooks.contract import Hooks, HookView
from mlops_agents.promotion.surfaces import GithubProtectionSurfaces
from mlops_agents.proposals.payloads import CommandSpec
from mlops_agents.workspace.refs import RepoRef
from tests.fakes.fake_github import FakeGitHubState, RunningFakeGitHub, start_fake_github
from tests.fakes.fake_tasks import FakeTasks


@pytest.fixture
def fake_github() -> Iterator[RunningFakeGitHub]:
    state = FakeGitHubState()
    running = start_fake_github(state)
    original_api = api.GITHUB_API
    api.GITHUB_API = running.base_url
    try:
        yield running
    finally:
        api.GITHUB_API = original_api
        running.stop()


class FakeGitHubAuth:
    def __init__(self, value: str = "test") -> None:
        self.value = value

    async def token(self) -> str | None:
        return self.value


class FakeHooks:
    def __init__(self, hooks: list[HookView] | None = None) -> None:
        self._hooks = hooks or []

    async def list(self, project_id: str, model: str | None = None) -> list[HookView]:
        if model is None:
            return list(self._hooks)
        return [h for h in self._hooks if h.model == model]


class TasksWithRepo(FakeTasks):
    def __init__(self, owner: str = "test-owner", repo: str = "test-repo") -> None:
        super().__init__()
        self._ref = RepoRef(owner, repo)

    async def repo_ref(self, project_id: str) -> RepoRef | None:
        return self._ref


def _make_hook(
    hook_id: str,
    name: str,
    kind: str,
    entry_files: list[str],
    model: str | None = "churn",
) -> HookView:
    spec = CommandSpec(
        kind=kind,  # type: ignore[arg-type]
        type="command",
        template="python main.py",
        workflow=None,
        inputs={},
        placeholders=[],
        deploys=(kind == "deploy"),
        entry_files=entry_files,
    )
    return HookView(
        id=hook_id,
        name=name,
        model=model,
        spec=spec,
        status="active",
        credential_set=False,
        source="proposal",
        created=datetime.now(UTC),
    )


def _make_surfaces(
    *,
    hooks: FakeHooks | None = None,
    http: httpx2.AsyncClient,
    github: FakeGitHubAuth | None = None,
    tasks: TasksWithRepo | None = None,
) -> GithubProtectionSurfaces:
    return GithubProtectionSurfaces(
        hooks=cast(Hooks, hooks or FakeHooks()),
        http=http,
        github=cast(GitHubAuth, github or FakeGitHubAuth()),
        tasks=cast(Tasks, tasks or TasksWithRepo()),
    )


async def test_protection_surfaces_paths() -> None:
    hooks = FakeHooks(
        [
            _make_hook("h1", "eval", "evaluate", ["eval/test.py"]),
            _make_hook("h2", "dep", "deploy", ["deploy/serve.py", "deploy/config.yaml"]),
            _make_hook("h3", "train", "train", ["train/train.py"]),  # Not evaluate or deploy
        ]
    )
    surfaces = _make_surfaces(
        hooks=hooks,
        http=httpx2.AsyncClient(),
    )

    paths = await surfaces.paths("proj_1")
    assert ".github/workflows/" in paths
    assert "CODEOWNERS" in paths
    assert ".github/CODEOWNERS" in paths
    assert "docs/CODEOWNERS" in paths
    assert "eval/test.py" in paths
    assert "deploy/serve.py" in paths
    assert "deploy/config.yaml" in paths
    assert "train/train.py" not in paths


async def test_protection_surfaces_touches_clean(fake_github: RunningFakeGitHub) -> None:
    repo = "test-owner/test-repo"
    async with httpx2.AsyncClient() as http:
        pr = await create_pull(
            http,
            token="ghp_test",
            repo=repo,
            title="Clean PR",
            head="feat",
            base="main",
        )
        fake_github.state.pr_files[(repo, pr.number)] = [
            {"filename": "src/models/churn.py", "status": "modified"}
        ]
        fake_github.state.prs[(repo, pr.number)]["changed_files"] = 1

        hooks = FakeHooks([_make_hook("h1", "eval", "evaluate", ["eval/test.py"])])
        surfaces = _make_surfaces(
            hooks=hooks,
            http=http,
        )

        touched = await surfaces.touches("proj_1", pr.number)
        assert touched == []


async def test_protection_surfaces_touches_workflows_and_codeowners(
    fake_github: RunningFakeGitHub,
) -> None:
    repo = "test-owner/test-repo"
    async with httpx2.AsyncClient() as http:
        pr = await create_pull(
            http,
            token="ghp_test",
            repo=repo,
            title="Touched PR",
            head="feat",
            base="main",
        )
        fake_github.state.pr_files[(repo, pr.number)] = [
            {"filename": ".github/workflows/ci.yml", "status": "modified"},
            {"filename": "docs/CODEOWNERS", "status": "modified"},
            {"filename": "src/safe.py", "status": "modified"},
        ]
        fake_github.state.prs[(repo, pr.number)]["changed_files"] = 3

        surfaces = _make_surfaces(
            http=http,
        )

        touched = await surfaces.touches("proj_1", pr.number)
        assert set(touched) == {".github/workflows/ci.yml", "docs/CODEOWNERS"}


async def test_protection_surfaces_touches_deploy_hook_and_rename_away(
    fake_github: RunningFakeGitHub,
) -> None:
    repo = "test-owner/test-repo"
    async with httpx2.AsyncClient() as http:
        pr = await create_pull(
            http,
            token="ghp_test",
            repo=repo,
            title="Rename hook away",
            head="feat",
            base="main",
        )
        fake_github.state.pr_files[(repo, pr.number)] = [
            {
                "filename": "backup/old_serve.py",
                "previous_filename": "deploy/serve.py",
                "status": "renamed",
            }
        ]
        fake_github.state.prs[(repo, pr.number)]["changed_files"] = 1

        hooks = FakeHooks([_make_hook("h_dep", "dep", "deploy", ["deploy/serve.py"])])
        surfaces = _make_surfaces(
            hooks=hooks,
            http=http,
        )

        touched = await surfaces.touches("proj_1", pr.number)
        assert "deploy/serve.py" in touched


async def test_protection_surfaces_refuses_when_files_exceed_github_return(
    fake_github: RunningFakeGitHub,
) -> None:
    repo = "test-owner/test-repo"
    async with httpx2.AsyncClient() as http:
        pr = await create_pull(
            http,
            token="ghp_test",
            repo=repo,
            title="Truncated PR",
            head="feat",
            base="main",
        )
        # GitHub reports 5 changed files, but list_pull_files only has 2
        fake_github.state.pr_files[(repo, pr.number)] = [
            {"filename": "file1.py", "status": "modified"},
            {"filename": "file2.py", "status": "modified"},
        ]
        fake_github.state.prs[(repo, pr.number)]["changed_files"] = 5

        surfaces = _make_surfaces(
            http=http,
        )

        with pytest.raises(DomainError) as exc_info:
            await surfaces.touches("proj_1", pr.number)
        assert exc_info.value.code == "surface_check_failed"


async def test_protection_surfaces_refuses_when_changed_files_is_none(
    fake_github: RunningFakeGitHub,
) -> None:
    repo = "test-owner/test-repo"
    async with httpx2.AsyncClient() as http:
        pr = await create_pull(
            http,
            token="ghp_test",
            repo=repo,
            title="PR without changed_files",
            head="feat",
            base="main",
        )
        fake_github.state.prs[(repo, pr.number)]["changed_files"] = None

        surfaces = _make_surfaces(
            http=http,
        )

        with pytest.raises(DomainError) as exc_info:
            await surfaces.touches("proj_1", pr.number)
        assert exc_info.value.code == "surface_check_failed"
        assert "unknown or missing" in exc_info.value.message
