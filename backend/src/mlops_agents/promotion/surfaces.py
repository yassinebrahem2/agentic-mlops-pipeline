"""Protection surfaces checks for pull requests (I38)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.pulls import get_pull, list_pull_files
from mlops_agents.promotion.contract import ProtectionSurfaces

if TYPE_CHECKING:
    import httpx2

    from mlops_agents.agents.tasks.contract import Tasks
    from mlops_agents.integrations.github.auth import GitHubAuth
    from mlops_agents.monitoring.hooks.contract import Hooks

STANDARD_PROTECTED_PREFIXES: tuple[str, ...] = (".github/workflows/",)
STANDARD_PROTECTED_FILES: tuple[str, ...] = (
    "CODEOWNERS",
    ".github/CODEOWNERS",
    "docs/CODEOWNERS",
)


class GithubProtectionSurfaces(ProtectionSurfaces):
    """Checks PR files against protected surfaces: workflows, CODEOWNERS, and evaluate/deploy hook entry files."""

    def __init__(
        self,
        hooks: Hooks,
        http: httpx2.AsyncClient,
        github: GitHubAuth,
        tasks: Tasks,
    ) -> None:
        self._hooks = hooks
        self._http = http
        self._github = github
        self._tasks = tasks

    async def paths(self, project_id: str) -> list[str]:
        result: list[str] = list(STANDARD_PROTECTED_PREFIXES) + list(STANDARD_PROTECTED_FILES)
        all_hooks = await self._hooks.list(project_id)
        for hook in all_hooks:
            if hook.spec.kind in ("evaluate", "deploy"):
                for entry_file in hook.spec.entry_files:
                    if entry_file not in result:
                        result.append(entry_file)
        return result

    async def touches(self, project_id: str, pr_number: int) -> list[str]:
        token = await self._github.token()
        if not token:
            raise DomainError(
                "github_not_configured", "GitHub integration is not configured.", status=422
            )
        ref = await self._tasks.repo_ref(project_id)
        if ref is None:
            raise DomainError(
                "github_not_configured",
                "No GitHub repository configured for project.",
                status=422,
            )
        repo_slug = f"{ref.owner}/{ref.repo}"

        pr = await get_pull(self._http, token, repo_slug, pr_number)
        pull_files = await list_pull_files(self._http, token, repo_slug, pr_number)

        # Hard limit: a list it cannot see whole is never trusted
        if pr.changed_files is None:
            raise DomainError(
                "surface_check_failed",
                "PR changed_files count is unknown or missing",
                status=422,
            )
        if len(pull_files) < pr.changed_files:
            raise DomainError(
                "surface_check_failed",
                f"PR #{pr_number} changed files ({pr.changed_files}) exceed files returned by GitHub ({len(pull_files)}).",
                status=422,
            )

        all_hooks = await self._hooks.list(project_id)
        hook_entry_files: set[str] = set()
        for hook in all_hooks:
            if hook.spec.kind in ("evaluate", "deploy"):
                hook_entry_files.update(hook.spec.entry_files)

        touched: set[str] = set()
        for pf in pull_files:
            candidates = [pf.filename]
            if pf.previous_filename:
                candidates.append(pf.previous_filename)

            for cand in candidates:
                if (
                    any(
                        cand == prefix.rstrip("/") or cand.startswith(prefix)
                        for prefix in STANDARD_PROTECTED_PREFIXES
                    )
                    or cand in STANDARD_PROTECTED_FILES
                    or cand in hook_entry_files
                ):
                    touched.add(cand)

        return sorted(touched)
