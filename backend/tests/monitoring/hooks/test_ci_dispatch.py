"""Tests for CI dispatcher and runner (Stage 5 Task 4, #489)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx2
import pytest

from mlops_agents.agents.approvals.authority import Authority
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github import api
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.dispatch import dispatch_workflow
from mlops_agents.integrations.github.git import GitResult, GitRunner
from mlops_agents.monitoring.hooks.ci_dispatch import CiDispatchRunner, GitHubCiDispatcher
from mlops_agents.monitoring.hooks.constants import HOOK_DETAIL_MAX_CHARS
from mlops_agents.monitoring.hooks.contract import (
    CiDispatcher,
    DispatchRef,
    HookView,
)
from mlops_agents.proposals.payloads import CommandSpec, Placeholder
from mlops_agents.workspace.models import Project
from mlops_agents.workspace.service import WorkspaceService
from tests.fakes.fake_github import FakeGitHubState, RunningFakeGitHub, start_fake_github


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


class FakeWorkspace:
    def __init__(
        self, projects: dict[str, Project] | None = None, root: Path | None = None
    ) -> None:
        self._projects = projects or {}
        self._root = root

    async def get(self, project_id: str) -> Project:
        if project_id not in self._projects:
            raise DomainError("project_not_found", "Project not found.", status=404)
        return self._projects[project_id]

    def repo_root(self, project: Project) -> Path:
        if self._root is not None:
            return self._root
        if project.local_path is not None:
            return Path(project.local_path)
        return Path.home() / "test-projects" / project.id


class FakeGitHubAuth:
    def __init__(self, token_val: str | None = None, *, unset: bool = False) -> None:
        self.token_val = None if unset else (token_val or "gh_tok")

    async def token(self) -> str | None:
        return self.token_val


class FakeGitRunner:
    def __init__(self, origin_url: str | None = None, code: int = 0) -> None:
        self.origin_url = origin_url
        self.code = code

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
        if "remote" in args or "remote.origin.url" in args:
            if self.origin_url:
                return GitResult(code=self.code, stdout=f"{self.origin_url}\n", stderr="")
            return GitResult(code=1, stdout="", stderr="fatal: No such remote 'origin'")
        return GitResult(code=0, stdout="", stderr="")


_AUTHORITY = Authority(basis="approval", ref="app_123", reason="approved")


def _make_project(
    project_id: str = "prj_1",
    *,
    source: str = "github",
    github_full_name: str | None = "octo/repo",
    default_branch: str | None = "main",
    local_path: str | None = None,
) -> Project:
    return Project(
        id=project_id,
        name="test-project",
        source="github" if source == "github" else "local",
        local_path=local_path,
        github_full_name=github_full_name,
        default_branch=default_branch,
        status="ok",
        status_detail=None,
        created_at="2026-10-31T00:00:00Z",
        last_opened_at=None,
    )


def _make_hook(
    hook_id: str = "hok_1",
    *,
    workflow: str = "train.yml",
    inputs: dict[str, str] | None = None,
    placeholders: list[Placeholder] | None = None,
) -> HookView:
    actual_inputs = (
        inputs if inputs is not None else {"model": "{model}", "window_start": "{window_start}"}
    )
    actual_placeholders = (
        placeholders
        if placeholders is not None
        else (["model", "window_start"] if actual_inputs else [])
    )
    return HookView(
        id=hook_id,
        name="Train hook",
        model="churn",
        spec=CommandSpec(
            kind="train",
            type="ci_dispatch",
            template=None,
            workflow=workflow,
            inputs=actual_inputs,
            placeholders=actual_placeholders,  # type: ignore[arg-type]
            deploys=False,
            entry_files=[f".github/workflows/{workflow}"],
        ),
        status="active",
        credential_set=True,
        pins=[],
        source="handoff",
        created=datetime(2026, 10, 31, 0, 0, 0, tzinfo=UTC),
    )


def _make_dispatcher(
    http: httpx2.AsyncClient,
    auth: FakeGitHubAuth,
    workspace: FakeWorkspace,
    git: FakeGitRunner | None = None,
) -> GitHubCiDispatcher:
    return GitHubCiDispatcher(
        http=http,
        github=cast(GitHubAuth, auth),
        workspace=cast(WorkspaceService, workspace),
        git=cast(GitRunner, git) if git is not None else None,
    )


def _make_runner(
    dispatcher: GitHubCiDispatcher,
    auth: FakeGitHubAuth | None = None,
) -> CiDispatchRunner:
    return CiDispatchRunner(
        dispatcher=dispatcher,
        github=cast(GitHubAuth, auth) if auth is not None else None,
    )


# ---------------------------------------------------------------------------
# dispatch_workflow unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_workflow_success(fake_github: RunningFakeGitHub) -> None:
    async with httpx2.AsyncClient() as http:
        await dispatch_workflow(
            http=http,
            token="test-token",
            repo="octo/repo",
            workflow="ci.yml",
            ref="main",
            inputs={"model": "churn", "mode": "train"},
        )

    state = fake_github.state
    assert len(state.dispatches) == 1
    assert state.dispatches[0] == {
        "repo": "octo/repo",
        "workflow": "ci.yml",
        "body": {
            "ref": "main",
            "inputs": {"model": "churn", "mode": "train"},
        },
    }
    # Check request headers recorded
    assert len(state.requests) == 1
    assert state.requests[0]["path"] == "/repos/octo/repo/actions/workflows/ci.yml/dispatches"


@pytest.mark.asyncio
async def test_dispatch_workflow_422_failure(fake_github: RunningFakeGitHub) -> None:
    fake_github.state.workflow_dispatch_errors[("octo/repo", "ci.yml")] = (
        422,
        {"message": "Workflow does not have 'workflow_dispatch' trigger"},
    )

    async with httpx2.AsyncClient() as http:
        with pytest.raises(DomainError) as exc_info:
            await dispatch_workflow(
                http=http,
                token="test-token",
                repo="octo/repo",
                workflow="ci.yml",
                ref="main",
                inputs={},
            )

    err = exc_info.value
    assert err.code == "workflow_dispatch_failed"
    assert err.status == 422
    assert "ci.yml" in err.message
    assert "Workflow does not have 'workflow_dispatch' trigger" in err.message


@pytest.mark.asyncio
async def test_dispatch_workflow_403_permission_missing(fake_github: RunningFakeGitHub) -> None:
    fake_github.state.simulate_permission_error["actions: write"] = 403

    async with httpx2.AsyncClient() as http:
        with pytest.raises(DomainError) as exc_info:
            await dispatch_workflow(
                http=http,
                token="test-token",
                repo="octo/repo",
                workflow="ci.yml",
                ref="main",
                inputs={},
            )

    err = exc_info.value
    assert err.code == "github_permission_missing"
    assert "actions: write" in err.message
    assert err.status == 403


@pytest.mark.asyncio
async def test_dispatch_workflow_401_rejected_token(fake_github: RunningFakeGitHub) -> None:
    fake_github.state.required_token = "good-token"

    async with httpx2.AsyncClient() as http:
        with pytest.raises(DomainError) as exc_info:
            await dispatch_workflow(
                http=http,
                token="bad-token",
                repo="octo/repo",
                workflow="ci.yml",
                ref="main",
                inputs={},
            )

    err = exc_info.value
    assert err.code == "github_token_rejected"


@pytest.mark.asyncio
async def test_dispatch_workflow_unexpected_status(fake_github: RunningFakeGitHub) -> None:
    fake_github.state.workflow_dispatch_errors[("octo/repo", "ci.yml")] = (
        500,
        {"message": "Internal error"},
    )

    async with httpx2.AsyncClient() as http:
        with pytest.raises(DomainError) as exc_info:
            await dispatch_workflow(
                http=http,
                token="test-token",
                repo="octo/repo",
                workflow="ci.yml",
                ref="main",
                inputs={},
            )

    err = exc_info.value
    assert err.code == "github_error"
    assert err.status == 502
    assert "500" in err.message


@pytest.mark.asyncio
async def test_dispatch_workflow_unreachable() -> None:
    original_api = api.GITHUB_API
    # Non-routable address
    api.GITHUB_API = "http://127.0.0.1:1"
    try:
        async with httpx2.AsyncClient() as http:
            with pytest.raises(DomainError) as exc_info:
                await dispatch_workflow(
                    http=http,
                    token="test-token",
                    repo="octo/repo",
                    workflow="ci.yml",
                    ref="main",
                    inputs={},
                )
        assert exc_info.value.code == "github_unreachable"
    finally:
        api.GITHUB_API = original_api


# ---------------------------------------------------------------------------
# GitHubCiDispatcher tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_ci_dispatcher_github_project(fake_github: RunningFakeGitHub) -> None:
    project = _make_project(
        "prj_gh", source="github", github_full_name="org/pipeline", default_branch="release-1"
    )
    workspace = FakeWorkspace({"prj_gh": project})
    auth = FakeGitHubAuth("secret-token-xyz")

    async with httpx2.AsyncClient() as http:
        dispatcher = _make_dispatcher(http, auth, workspace)
        ref = await dispatcher.dispatch("prj_gh", "retrain.yaml", {"k": "v"})

    assert ref == DispatchRef(workflow="retrain.yaml", run_url=None)
    assert len(fake_github.state.dispatches) == 1
    dispatch = fake_github.state.dispatches[0]
    assert dispatch["repo"] == "org/pipeline"
    assert dispatch["workflow"] == "retrain.yaml"
    assert dispatch["body"]["ref"] == "release-1"
    assert dispatch["body"]["inputs"] == {"k": "v"}


@pytest.mark.asyncio
async def test_github_ci_dispatcher_local_project_with_remote(
    fake_github: RunningFakeGitHub, tmp_path: Path
) -> None:
    project = _make_project(
        "prj_local",
        source="local",
        github_full_name=None,
        default_branch="dev",
        local_path=str(tmp_path),
    )
    workspace = FakeWorkspace({"prj_local": project}, root=tmp_path)
    auth = FakeGitHubAuth("secret-token-xyz")
    git = FakeGitRunner(origin_url="git@github.com:octo/my-local-project.git")

    async with httpx2.AsyncClient() as http:
        dispatcher = _make_dispatcher(http, auth, workspace, git)
        ref = await dispatcher.dispatch("prj_local", "train.yml", {"epochs": "10"})

    assert ref == DispatchRef(workflow="train.yml", run_url=None)
    assert len(fake_github.state.dispatches) == 1
    dispatch = fake_github.state.dispatches[0]
    assert dispatch["repo"] == "octo/my-local-project"
    assert dispatch["body"]["ref"] == "dev"
    assert dispatch["body"]["inputs"] == {"epochs": "10"}


@pytest.mark.asyncio
async def test_github_ci_dispatcher_no_remote(tmp_path: Path) -> None:
    project = _make_project(
        "prj_no_remote",
        source="local",
        github_full_name=None,
        default_branch="main",
        local_path=str(tmp_path),
    )
    workspace = FakeWorkspace({"prj_no_remote": project}, root=tmp_path)
    auth = FakeGitHubAuth("secret-token-xyz")
    git = FakeGitRunner(origin_url=None)

    async with httpx2.AsyncClient() as http:
        dispatcher = _make_dispatcher(http, auth, workspace, git)
        with pytest.raises(DomainError) as exc_info:
            await dispatcher.dispatch("prj_no_remote", "train.yml", {})

    err = exc_info.value
    assert err.code == "no_github_remote"
    assert err.status == 400
    assert (
        err.message
        == "The pipeline cannot be started until a CI workflow or a remote command exists."
    )


@pytest.mark.asyncio
async def test_github_ci_dispatcher_github_not_connected() -> None:
    project = _make_project("prj_1", github_full_name="octo/repo")
    workspace = FakeWorkspace({"prj_1": project})
    auth = FakeGitHubAuth(unset=True)

    async with httpx2.AsyncClient() as http:
        dispatcher = _make_dispatcher(http, auth, workspace)
        with pytest.raises(DomainError) as exc_info:
            await dispatcher.dispatch("prj_1", "train.yml", {})

    err = exc_info.value
    assert err.code == "github_not_connected"
    assert err.status == 409
    assert err.message == "Connect GitHub in Integrations first."


@pytest.mark.asyncio
async def test_github_ci_dispatcher_hard_constraint_ref_defaults_to_main(
    fake_github: RunningFakeGitHub,
) -> None:
    # Project with default_branch=None defaults to "main"
    project = _make_project("prj_default_none", github_full_name="octo/repo", default_branch=None)
    workspace = FakeWorkspace({"prj_default_none": project})
    auth = FakeGitHubAuth("token")

    async with httpx2.AsyncClient() as http:
        dispatcher = _make_dispatcher(http, auth, workspace)
        await dispatcher.dispatch("prj_default_none", "train.yml", {})

    assert fake_github.state.dispatches[0]["body"]["ref"] == "main"


# ---------------------------------------------------------------------------
# CiDispatchRunner tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ci_dispatch_runner_success(fake_github: RunningFakeGitHub) -> None:
    project = _make_project("prj_1", github_full_name="octo/repo", default_branch="main")
    workspace = FakeWorkspace({"prj_1": project})
    auth = FakeGitHubAuth("secret-token-xyz")

    async with httpx2.AsyncClient() as http:
        dispatcher = _make_dispatcher(http, auth, workspace)
        runner = _make_runner(dispatcher, auth)

        hook = _make_hook(
            "hok_1",
            workflow="retrain.yml",
            inputs={"model": "{model}", "window_start": "{window_start}"},
        )
        params = {
            "model": "churn",
            "window_start": "2026-01-01T00:00:00Z",
            "request_id": "trq_001",
        }
        authority = _AUTHORITY
        res = await runner.run("prj_1", hook, params, authority)

    assert res.ok is True
    assert res.detail == "Dispatched workflow 'retrain.yml'"
    assert res.ci_run_url is None
    assert res.request_id == "trq_001"

    assert len(fake_github.state.dispatches) == 1
    dispatch = fake_github.state.dispatches[0]
    assert dispatch["repo"] == "octo/repo"
    assert dispatch["workflow"] == "retrain.yml"
    assert dispatch["body"]["ref"] == "main"
    assert dispatch["body"]["inputs"] == {
        "model": "churn",
        "window_start": "2026-01-01T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_ci_dispatch_runner_cannot_override_ref(fake_github: RunningFakeGitHub) -> None:
    project = _make_project(
        "prj_1", github_full_name="octo/repo", default_branch="protected_branch"
    )
    workspace = FakeWorkspace({"prj_1": project})
    auth = FakeGitHubAuth("secret-token-xyz")

    async with httpx2.AsyncClient() as http:
        dispatcher = _make_dispatcher(http, auth, workspace)
        runner = _make_runner(dispatcher, auth)

        hook = _make_hook("hok_1", workflow="retrain.yml", inputs={"foo": "bar"})
        # Attempting to pass ref or branch in params
        params = {
            "ref": "attacker_branch",
            "branch": "malicious_branch",
            "request_id": "trq_002",
        }
        authority = _AUTHORITY
        res = await runner.run("prj_1", hook, params, authority)

    assert res.ok is True
    assert fake_github.state.dispatches[0]["body"]["ref"] == "protected_branch"


@pytest.mark.asyncio
async def test_ci_dispatch_runner_422_failure_names_workflow_without_leaking_secrets(
    fake_github: RunningFakeGitHub,
) -> None:
    token_secret = "ghp_very_secret_token_12345"
    project = _make_project("prj_1", github_full_name="octo/repo")
    workspace = FakeWorkspace({"prj_1": project})
    auth = FakeGitHubAuth(token_secret)

    fake_github.state.workflow_dispatch_errors[("octo/repo", "retrain.yml")] = (
        422,
        {"message": f"Workflow does not have trigger with token {token_secret}"},
    )

    async with httpx2.AsyncClient() as http:
        dispatcher = _make_dispatcher(http, auth, workspace)
        runner = _make_runner(dispatcher, auth)

        hook = _make_hook("hok_1", workflow="retrain.yml", inputs={})
        authority = _AUTHORITY
        res = await runner.run("prj_1", hook, {}, authority)

    assert res.ok is False
    assert "retrain.yml" in res.detail
    assert "Workflow does not have trigger" in res.detail
    assert token_secret not in res.detail
    assert "***" in res.detail


@pytest.mark.asyncio
async def test_ci_dispatch_runner_403_permission_missing_without_leaking_secrets(
    fake_github: RunningFakeGitHub,
) -> None:
    token_secret = "ghp_very_secret_token_abcde"
    project = _make_project("prj_1", github_full_name="octo/repo")
    workspace = FakeWorkspace({"prj_1": project})
    auth = FakeGitHubAuth(token_secret)
    fake_github.state.simulate_permission_error["actions: write"] = 403

    async with httpx2.AsyncClient() as http:
        dispatcher = _make_dispatcher(http, auth, workspace)
        runner = _make_runner(dispatcher, auth)

        hook = _make_hook("hok_1", workflow="retrain.yml", inputs={})
        authority = _AUTHORITY
        res = await runner.run("prj_1", hook, {}, authority)

    assert res.ok is False
    assert "actions: write" in res.detail
    assert token_secret not in res.detail


@pytest.mark.asyncio
async def test_ci_dispatch_runner_unreachable_github_without_leaking_secrets() -> None:
    original_api = api.GITHUB_API
    api.GITHUB_API = "http://127.0.0.1:1"
    token_secret = "ghp_very_secret_token_unreach"
    project = _make_project("prj_1", github_full_name="octo/repo")
    workspace = FakeWorkspace({"prj_1": project})
    auth = FakeGitHubAuth(token_secret)

    try:
        async with httpx2.AsyncClient() as http:
            dispatcher = _make_dispatcher(http, auth, workspace)
            runner = _make_runner(dispatcher, auth)

            hook = _make_hook("hok_1", workflow="retrain.yml", inputs={})
            authority = _AUTHORITY
            res = await runner.run("prj_1", hook, {}, authority)

        assert res.ok is False
        assert "Could not reach GitHub" in res.detail
        assert token_secret not in res.detail
    finally:
        api.GITHUB_API = original_api


@pytest.mark.asyncio
async def test_ci_dispatch_runner_no_remote_gives_exact_message(tmp_path: Path) -> None:
    project = _make_project(
        "prj_local",
        source="local",
        github_full_name=None,
        local_path=str(tmp_path),
    )
    workspace = FakeWorkspace({"prj_local": project}, root=tmp_path)
    auth = FakeGitHubAuth("token")
    git = FakeGitRunner(origin_url=None)

    async with httpx2.AsyncClient() as http:
        dispatcher = _make_dispatcher(http, auth, workspace, git)
        runner = _make_runner(dispatcher, auth)

        hook = _make_hook("hok_1", workflow="retrain.yml", inputs={})
        authority = _AUTHORITY
        res = await runner.run("prj_local", hook, {}, authority)

    assert res.ok is False
    assert (
        res.detail
        == "The pipeline cannot be started until a CI workflow or a remote command exists."
    )


@pytest.mark.asyncio
async def test_ci_dispatch_runner_not_connected_gives_exact_message() -> None:
    project = _make_project("prj_1", github_full_name="octo/repo")
    workspace = FakeWorkspace({"prj_1": project})
    auth = FakeGitHubAuth(unset=True)

    async with httpx2.AsyncClient() as http:
        dispatcher = _make_dispatcher(http, auth, workspace)
        runner = _make_runner(dispatcher, auth)

        hook = _make_hook("hok_1", workflow="retrain.yml", inputs={})
        authority = _AUTHORITY
        res = await runner.run("prj_1", hook, {}, authority)

    assert res.ok is False
    assert res.detail == "Connect GitHub in Integrations first."


@pytest.mark.asyncio
async def test_ci_dispatch_runner_truncates_long_detail() -> None:
    class FailingDispatcher(CiDispatcher):
        async def dispatch(
            self, project_id: str, workflow: str, inputs: dict[str, str]
        ) -> DispatchRef:
            raise DomainError("huge_error", "A" * (HOOK_DETAIL_MAX_CHARS + 500))

    runner = CiDispatchRunner(dispatcher=FailingDispatcher())
    hook = _make_hook("hok_1")
    authority = _AUTHORITY
    res = await runner.run("prj_1", hook, {"model": "m", "window_start": "w"}, authority)

    assert res.ok is False
    assert len(res.detail) == HOOK_DETAIL_MAX_CHARS
    assert res.detail.startswith("AAA")
