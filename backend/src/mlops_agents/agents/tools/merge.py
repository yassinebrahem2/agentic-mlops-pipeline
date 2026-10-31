"""MergePullRequest tool (I38)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from mlops_agents.agents.approvals.authority import ActionCall
from mlops_agents.agents.tools.base import (
    AgentTool,
    Preview,
    RiskClass,
    ToolContext,
    ToolFailure,
    ToolOutput,
)
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.merge import (
    PullMergeDetails,
    get_check_runs,
    get_pull_merge_state,
    merge_pull_request,
)

if TYPE_CHECKING:
    import httpx2

    from mlops_agents.agents.pr_sync import PrSyncObserver
    from mlops_agents.agents.runtime.contract import TaskView
    from mlops_agents.agents.tasks.contract import Tasks
    from mlops_agents.events.contract import EventLog
    from mlops_agents.integrations.github.auth import GitHubAuth
    from mlops_agents.promotion.contract import ProtectionSurfaces

PASSING_CONCLUSIONS: frozenset[str] = frozenset({"success", "neutral", "skipped"})


class MergePullRequestArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pr_number: int = Field(description="Pull request number to merge.")


class MergePullRequestTool(AgentTool[MergePullRequestArgs]):
    """Merge a clean GitHub pull request belonging to the calling agent."""

    name: ClassVar[str] = "MergePullRequest"
    description: ClassVar[str] = "Merge a clean GitHub pull request belonging to the calling agent."
    risk: ClassVar[RiskClass] = RiskClass.DEPLOY
    args_model: ClassVar[type[BaseModel]] = MergePullRequestArgs

    def __init__(
        self,
        tasks: Tasks,
        github: GitHubAuth,
        http: httpx2.AsyncClient,
        events: EventLog,
        surfaces: ProtectionSurfaces,
        pr_sync: PrSyncObserver,
    ) -> None:
        self._tasks = tasks
        self._github = github
        self._http = http
        self._events = events
        self._surfaces = surfaces
        self._pr_sync = pr_sync

    async def action_call(self, args: MergePullRequestArgs, ctx: ToolContext) -> ActionCall | None:
        return ActionCall(project_id=ctx.project_id, agent=ctx.agent, kind="merge_pr")

    async def _check_merge(self, ctx: ToolContext, pr_number: int) -> tuple[TaskView, str]:
        refusals: list[str] = []

        # 1. Own PR & review kind check
        open_tasks = await self._tasks.open_tasks(ctx.project_id)
        matching_task: TaskView | None = None
        for t in open_tasks:
            if t.pr_number == pr_number:
                matching_task = t
                break

        if matching_task is None:
            refusals.append(f"PR #{pr_number} is not an open PR for project {ctx.project_id}.")
        else:
            if matching_task.agent != ctx.agent:
                refusals.append(
                    f"PR #{pr_number} is not owned by calling agent '{ctx.agent}' (owned by '{matching_task.agent}')."
                )
            if matching_task.review_kind != "github":
                refusals.append(
                    f"PR #{pr_number} has review_kind {matching_task.review_kind!r}; local reviews cannot be merged with MergePullRequest."
                )

        # 2. GitHub state & check runs
        token = await self._github.token()
        if not token:
            refusals.append("GitHub integration is not configured.")
        ref = await self._tasks.repo_ref(ctx.project_id)
        if ref is None:
            refusals.append("No GitHub repository configured for project.")

        pr_details: PullMergeDetails | None = None
        if token and ref:
            repo_slug = f"{ref.owner}/{ref.repo}"
            try:
                pr_details = await get_pull_merge_state(self._http, token, repo_slug, pr_number)
            except Exception as exc:
                refusals.append(f"Could not retrieve merge state for PR #{pr_number}: {exc}")

            if pr_details is not None:
                if not pr_details.mergeable:
                    refusals.append(f"PR #{pr_number} is not mergeable.")
                if pr_details.mergeable_state != "clean":
                    refusals.append(
                        f"PR #{pr_number} mergeable_state is '{pr_details.mergeable_state}', expected 'clean'."
                    )

                try:
                    runs = await get_check_runs(self._http, token, repo_slug, pr_details.head_sha)
                except Exception as exc:
                    refusals.append(
                        f"Could not retrieve check runs for head {pr_details.head_sha}: {exc}"
                    )
                    runs = []

                passing_runs = [
                    r
                    for r in runs
                    if r.status == "completed" and r.conclusion in PASSING_CONCLUSIONS
                ]
                failed_or_pending = [
                    r
                    for r in runs
                    if r.status != "completed" or r.conclusion not in PASSING_CONCLUSIONS
                ]

                if not runs:
                    refusals.append(f"Head commit {pr_details.head_sha} has no check runs.")
                elif not passing_runs:
                    refusals.append(f"Head commit {pr_details.head_sha} has no passing check runs.")

                if failed_or_pending:
                    names = ", ".join(
                        f"{r.name} ({r.status}/{r.conclusion})" for r in failed_or_pending
                    )
                    refusals.append(
                        f"Head commit {pr_details.head_sha} has failed or pending check runs: {names}."
                    )

        # 3. Protected surfaces
        try:
            touched = await self._surfaces.touches(ctx.project_id, pr_number)
            if touched:
                refusals.append(f"PR touches protected surfaces: {', '.join(sorted(touched))}.")
        except DomainError as exc:
            refusals.append(str(exc))
        except Exception as exc:
            refusals.append(f"Could not verify protected surfaces: {exc}")

        if refusals or matching_task is None or pr_details is None:
            raise ToolFailure("\n".join(refusals) if refusals else "PR merge checks failed.")

        return matching_task, pr_details.head_sha

    async def check_merge(self, ctx: ToolContext, pr_number: int) -> tuple[TaskView, str]:
        """Check all merge preconditions and return task and head sha, or raise ToolFailure."""
        return await self._check_merge(ctx, pr_number)

    async def preview(self, ctx: ToolContext, args: MergePullRequestArgs) -> Preview:
        task, head_sha = await self.check_merge(ctx, args.pr_number)
        return Preview(
            kind="text",
            title=f"Merge PR #{args.pr_number}",
            body=f"Merge pull request #{args.pr_number} (head sha: {head_sha}) for task {task.id}.",
        )

    async def run(self, ctx: ToolContext, args: MergePullRequestArgs) -> ToolOutput:
        task, head_sha = await self.check_merge(ctx, args.pr_number)
        token = await self._github.token()
        ref = await self._tasks.repo_ref(ctx.project_id)
        if token is None or ref is None:
            raise ToolFailure("GitHub integration or repository ref not configured.")
        repo_slug = f"{ref.owner}/{ref.repo}"

        await merge_pull_request(self._http, token, repo_slug, args.pr_number, sha=head_sha)
        await self._events.record(
            ctx.project_id,
            source=ctx.agent,
            type="pr.merged_by_agent",
            subject_id=task.id,
            payload={"pr_number": args.pr_number, "sha": head_sha},
        )
        await self._pr_sync.sync_now(ctx.project_id)
        return ToolOutput(content=f"Pull request #{args.pr_number} merged successfully.")


MergePullRequest = MergePullRequestTool
