"""CI dispatch hook runner and dispatcher for GitHub Actions."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

import httpx2

from mlops_agents.agents.approvals.authority import Authority
from mlops_agents.credentials.scrub import scrub_url_secrets, scrub_values
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.dispatch import dispatch_workflow
from mlops_agents.monitoring.hooks.constants import HOOK_DETAIL_MAX_CHARS
from mlops_agents.monitoring.hooks.contract import (
    CiDispatcher,
    DispatchRef,
    HookResult,
    HookRunner,
    HookView,
)
from mlops_agents.proposals.templates import fill_template
from mlops_agents.workspace.refs import RepoRef, github_remote

if TYPE_CHECKING:
    from mlops_agents.integrations.github.git import GitRunner
    from mlops_agents.workspace.service import WorkspaceService


class GitHubCiDispatcher(CiDispatcher):
    """Dispatches GitHub Actions workflows for a project."""

    def __init__(
        self,
        http: httpx2.AsyncClient,
        github: GitHubAuth,
        workspace: WorkspaceService,
        git: GitRunner | None = None,
    ) -> None:
        self._http = http
        self._github = github
        self._workspace = workspace
        self._git = git

    async def dispatch(self, project_id: str, workflow: str, inputs: dict[str, str]) -> DispatchRef:
        if not workflow:
            raise DomainError(
                "missing_workflow",
                "The hook does not specify a workflow to dispatch.",
                status=400,
            )

        project = await self._workspace.get(project_id)
        repo: RepoRef | None = None
        if project.github_full_name:
            repo = github_remote(project, None)
        elif project.source == "local":
            origin_url: str | None = None
            if self._git is not None:
                repo_root = self._workspace.repo_root(project)
                if repo_root.is_dir():
                    res = await self._git.run(
                        ["remote", "get-url", "origin"], cwd=repo_root, check=False
                    )
                    if res.code == 0 and res.stdout.strip():
                        origin_url = res.stdout.strip()
                    else:
                        res = await self._git.run(
                            ["config", "--get", "remote.origin.url"], cwd=repo_root, check=False
                        )
                        if res.code == 0 and res.stdout.strip():
                            origin_url = res.stdout.strip()
            repo = github_remote(project, origin_url)

        if repo is None:
            raise DomainError(
                "no_github_remote",
                "The pipeline cannot be started until a CI workflow or a remote command exists.",
                status=400,
            )

        token = await self._github.token()
        if token is None:
            raise DomainError(
                "github_not_connected",
                "Connect GitHub in Integrations first.",
                status=409,
            )

        ref = project.default_branch or "main"
        await dispatch_workflow(
            self._http,
            token,
            f"{repo.owner}/{repo.repo}",
            workflow,
            ref,
            inputs,
        )
        return DispatchRef(workflow=workflow, run_url=None)


class CiDispatchRunner(HookRunner):
    """Executes a CI dispatch hook via GitHub Actions."""

    type: Literal["command", "ci_dispatch"] = "ci_dispatch"

    def __init__(
        self,
        dispatcher: CiDispatcher,
        github: GitHubAuth | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._github = github

    async def run(
        self,
        project_id: str,
        hook: HookView,
        params: Mapping[str, str],
        authority: Authority,
    ) -> HookResult:
        request_id = params.get("request_id")
        try:
            workflow = hook.spec.workflow or ""
            inputs = {
                k: fill_template(v, dict(params)) for k, v in (hook.spec.inputs or {}).items()
            }
            ref = await self._dispatcher.dispatch(project_id, workflow, inputs)
            return HookResult(
                ok=True,
                detail=f"Dispatched workflow '{workflow}'",
                ci_run_url=ref.run_url,
                request_id=request_id,
            )
        except Exception as exc:
            raw_detail = exc.message if isinstance(exc, DomainError) else str(exc)
            if not raw_detail:
                raw_detail = f"Failed to dispatch workflow: {type(exc).__name__}"
            secrets: list[str] = []
            if self._github is not None:
                with contextlib.suppress(Exception):
                    tok = await self._github.token()
                    if tok:
                        secrets.append(tok)
            detail = scrub_values(scrub_url_secrets(raw_detail), secrets)
            if len(detail) > HOOK_DETAIL_MAX_CHARS:
                detail = detail[:HOOK_DETAIL_MAX_CHARS]
            return HookResult(
                ok=False,
                detail=detail,
                ci_run_url=None,
                request_id=request_id,
            )
