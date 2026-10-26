from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, Field

from mlops_agents.agents.tasks.contract import CommitRefused
from mlops_agents.agents.tools.base import (
    AgentTool,
    Preview,
    RiskClass,
    ToolContext,
    ToolOutput,
)
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.git import GitError
from mlops_agents.integrations.github.pulls import create_pull, update_pull
from mlops_agents.workspace.refs import github_remote, repo_ref

if TYPE_CHECKING:
    import httpx2

    from mlops_agents.agents.tasks.contract import Tasks
    from mlops_agents.agents.tools.base import TaskRef
    from mlops_agents.events.contract import EventLog
    from mlops_agents.integrations.github.auth import GitHubAuth
    from mlops_agents.workspace.models import Project
    from mlops_agents.workspace.refs import RepoRef
    from mlops_agents.workspace.service import WorkspaceService


class OpenPullRequestArgs(BaseModel):
    title: str = Field(description="Title of the pull request.")
    body: str = Field(description="Summary of the changes made.")
    fixes: list[int] = Field(default_factory=list[int], description="Issue numbers this PR fixes.")


class OpenPullRequestTool(AgentTool[OpenPullRequestArgs]):
    name: ClassVar[str] = "OpenPullRequest"
    description: ClassVar[str] = (
        "Open a pull request on GitHub or create a local review for the current task."
    )
    risk: ClassVar[RiskClass] = RiskClass.GITHUB
    args_model: ClassVar[type[BaseModel]] = OpenPullRequestArgs

    def __init__(
        self,
        tasks: Tasks,
        workspace: WorkspaceService,
        github: GitHubAuth,
        events: EventLog,
        http: httpx2.AsyncClient,
    ) -> None:
        self._tasks = tasks
        self._workspace = workspace
        self._github = github
        self._events = events
        self._http = http

    async def preview(self, ctx: ToolContext, args: OpenPullRequestArgs) -> Preview:
        if ctx.task is None:
            return Preview(kind="diff", title=args.title, body="")
        diff = await self._tasks.staged_diff(ctx.project_id, ctx.task)
        return Preview(kind="diff", title=args.title, body=diff)

    async def _resolve_repo_ref(self, project: Project, task: TaskRef) -> RepoRef | None:
        if project.github_full_name:
            return repo_ref(project.github_full_name)

        git = getattr(self._workspace, "_git", None)
        if git is not None:
            try:
                repo_root = self._workspace.repo_root(project)
            except Exception:
                repo_root = task.worktree
            res = await git.run(
                ["config", "--get", "remote.origin.url"], cwd=repo_root, check=False
            )
            origin_url = res.stdout.strip() if res.code == 0 else None
            if not origin_url:
                res = await git.run(["remote", "get-url", "origin"], cwd=repo_root, check=False)
                origin_url = res.stdout.strip() if res.code == 0 else None
            return github_remote(project, origin_url)

        return github_remote(project, None)

    async def run(self, ctx: ToolContext, args: OpenPullRequestArgs) -> ToolOutput:
        if ctx.task is None:
            return ToolOutput(content="Start a task first with StartTask.", is_error=True)

        footer_lines = [f"Opened by {ctx.agent} from task {ctx.task.id}."]
        for fix_num in args.fixes:
            footer_lines.append(f"Fixes #{fix_num}")
        footer = "\n".join(footer_lines)
        stripped_body = args.body.strip()
        formatted_body = f"{stripped_body}\n\n{footer}" if stripped_body else footer

        project = await self._workspace.get(ctx.project_id)
        ref = await self._resolve_repo_ref(project, ctx.task)

        if ref is not None:
            try:
                await self._tasks.commit(ctx.project_id, ctx.task, args.title)
            except CommitRefused as err:
                return ToolOutput(content=str(err), is_error=True)

            try:
                await self._tasks.push(ctx.project_id, ctx.task)
            except (DomainError, GitError) as err:
                return ToolOutput(content=str(err), is_error=True)

            task_view = await self._tasks.view(ctx.project_id, ctx.task.id)
            token = await self._github.token()
            if not token:
                return ToolOutput(content="GitHub integration is not configured.", is_error=True)

            repo_full_name = f"{ref.owner}/{ref.repo}"
            base_branch = project.default_branch or "main"

            if task_view.pr_number is None:
                try:
                    pr = await create_pull(
                        self._http,
                        token,
                        repo_full_name,
                        title=args.title,
                        head=ctx.task.branch,
                        base=base_branch,
                        body=formatted_body,
                    )
                except DomainError as err:
                    return ToolOutput(content=str(err), is_error=True)

                await self._tasks.set_review(
                    ctx.project_id,
                    ctx.task.id,
                    kind="github",
                    pr_number=pr.number,
                    pr_url=pr.html_url,
                )
                await self._events.record(
                    ctx.project_id,
                    source=ctx.agent,
                    type="pr.opened",
                    payload={
                        "task_id": ctx.task.id,
                        "number": pr.number,
                        "url": pr.html_url,
                        "title": pr.title,
                    },
                    subject_id=ctx.task.id,
                )
                return ToolOutput(content=f"Pull request #{pr.number} opened: {pr.html_url}")
            else:
                try:
                    pr = await update_pull(
                        self._http,
                        token,
                        repo_full_name,
                        task_view.pr_number,
                        title=args.title,
                        body=formatted_body,
                    )
                except DomainError as err:
                    return ToolOutput(content=str(err), is_error=True)

                await self._tasks.set_review(
                    ctx.project_id,
                    ctx.task.id,
                    kind="github",
                    pr_number=pr.number,
                    pr_url=pr.html_url,
                )
                await self._events.record(
                    ctx.project_id,
                    source=ctx.agent,
                    type="pr.updated",
                    payload={
                        "task_id": ctx.task.id,
                        "number": pr.number,
                        "url": pr.html_url,
                        "title": pr.title,
                    },
                    subject_id=ctx.task.id,
                )
                return ToolOutput(content=f"Pull request #{pr.number} updated: {pr.html_url}")

        # Case B: Project has NO RepoRef -> local review
        try:
            await self._tasks.commit(ctx.project_id, ctx.task, args.title)
        except CommitRefused as err:
            return ToolOutput(content=str(err), is_error=True)

        await self._tasks.set_review(ctx.project_id, ctx.task.id, kind="local")
        return ToolOutput(content=f"Local review opened for task '{ctx.task.title}'.")
