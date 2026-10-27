from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal, cast

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from mlops_agents.agents.tools.base import (
    AgentTool,
    Preview,
    RiskClass,
    ToolContext,
    ToolOutput,
)
from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.issues import (
    comment_issue,
    create_issue,
    ensure_labels,
    get_issue,
    search_issues,
)
from mlops_agents.storage.tables import project_settings

if TYPE_CHECKING:
    import httpx2

    from mlops_agents.agents.tasks.contract import Tasks
    from mlops_agents.events.contract import EventLog
    from mlops_agents.integrations.github.auth import GitHubAuth
    from mlops_agents.storage.databases import Databases

IssuePriority = Literal["low", "medium", "high", "critical"]

PRIORITY_RANKS: dict[str, int] = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

DEFAULT_MIN_PRIORITY: IssuePriority = "medium"
SETTING_KEY = "issue_min_priority"


async def get_issue_min_priority(databases: Databases, project_id: str) -> IssuePriority:
    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        value = (
            await conn.execute(
                select(project_settings.c.value).where(project_settings.c.key == SETTING_KEY)
            )
        ).scalar_one_or_none()
    if value in PRIORITY_RANKS:
        return cast(IssuePriority, value)
    return DEFAULT_MIN_PRIORITY


async def set_issue_min_priority(
    databases: Databases, project_id: str, priority: IssuePriority
) -> IssuePriority:
    engine = await databases.project(project_id)
    statement = insert(project_settings).values(key=SETTING_KEY, value=priority)
    statement = statement.on_conflict_do_update(index_elements=["key"], set_={"value": priority})
    async with engine.begin() as conn:
        await conn.execute(statement)
    return priority


class SearchIssuesArgs(BaseModel):
    query: str = Field(default="", description="Search query string.")


class SearchIssuesTool(AgentTool[SearchIssuesArgs]):
    name: ClassVar[str] = "SearchIssues"
    description: ClassVar[str] = "Search open issues in the project's GitHub repository."
    risk: ClassVar[RiskClass] = RiskClass.READ
    args_model: ClassVar[type[BaseModel]] = SearchIssuesArgs

    def __init__(
        self,
        tasks: Tasks,
        github: GitHubAuth,
        http: httpx2.AsyncClient,
    ) -> None:
        self._tasks = tasks
        self._github = github
        self._http = http

    async def run(self, ctx: ToolContext, args: SearchIssuesArgs) -> ToolOutput:
        ref = await self._tasks.repo_ref(ctx.project_id)
        if ref is None:
            return ToolOutput(content="Project has no GitHub link.", is_error=True)

        token = await self._github.token()
        if not token:
            return ToolOutput(content="GitHub integration is not configured.", is_error=True)

        repo_full_name = f"{ref.owner}/{ref.repo}"
        try:
            issues = await search_issues(self._http, token, repo_full_name, query=args.query)
        except DomainError as err:
            return ToolOutput(content=str(err), is_error=True)

        if not issues:
            return ToolOutput(content="No open issues found.")

        lines: list[str] = []
        for issue in issues:
            labels_str = f" [{', '.join(issue.labels)}]" if issue.labels else ""
            lines.append(
                f"#{issue.number} {issue.title} ({issue.state}){labels_str}\n  {issue.html_url}"
            )
        return ToolOutput(content="\n".join(lines))


class OpenIssueArgs(BaseModel):
    title: str = Field(description="Title of the issue.")
    body: str = Field(description="Body of the issue.")
    category: str = Field(description="Category of the issue.")
    priority: IssuePriority = Field(description="Priority of the issue.")


class OpenIssueTool(AgentTool[OpenIssueArgs]):
    name: ClassVar[str] = "OpenIssue"
    description: ClassVar[str] = "Open an issue on GitHub for the project."
    risk: ClassVar[RiskClass] = RiskClass.GITHUB
    args_model: ClassVar[type[BaseModel]] = OpenIssueArgs

    def __init__(
        self,
        tasks: Tasks,
        github: GitHubAuth,
        events: EventLog,
        http: httpx2.AsyncClient,
        databases: Databases,
    ) -> None:
        self._tasks = tasks
        self._github = github
        self._events = events
        self._http = http
        self._databases = databases

    async def preview(self, ctx: ToolContext, args: OpenIssueArgs) -> Preview:
        return Preview(kind="text", title=args.title, body=args.body)

    async def run(self, ctx: ToolContext, args: OpenIssueArgs) -> ToolOutput:
        ref = await self._tasks.repo_ref(ctx.project_id)
        if ref is None:
            return ToolOutput(content="Project has no GitHub link.", is_error=True)

        min_priority = await get_issue_min_priority(self._databases, ctx.project_id)
        if PRIORITY_RANKS.get(args.priority, 0) < PRIORITY_RANKS.get(min_priority, 2):
            return ToolOutput(
                content=(
                    f"Issue priority '{args.priority}' is below the project's minimum '{min_priority}'. "
                    "Report this in your session instead."
                ),
                is_error=True,
            )

        token = await self._github.token()
        if not token:
            return ToolOutput(content="GitHub integration is not configured.", is_error=True)

        repo_full_name = f"{ref.owner}/{ref.repo}"
        labels = [f"agent:{ctx.agent}", f"category:{args.category}", f"priority:{args.priority}"]

        try:
            await ensure_labels(self._http, token, repo_full_name, labels)
            issue = await create_issue(
                self._http,
                token,
                repo_full_name,
                title=args.title,
                body=args.body,
                labels=labels,
            )
        except DomainError as err:
            return ToolOutput(content=str(err), is_error=True)

        await self._events.record(
            ctx.project_id,
            source=ctx.agent,
            type="issue.opened",
            payload={
                **issue.event_payload(),
                "category": args.category,
                "priority": args.priority,
            },
            subject_id=str(issue.number),
        )

        return ToolOutput(content=f"Issue #{issue.number} opened: {issue.html_url}")


class CommentOnIssueArgs(BaseModel):
    number: int = Field(description="Issue number to comment on.")
    body: str = Field(description="Comment body.")


class CommentOnIssueTool(AgentTool[CommentOnIssueArgs]):
    name: ClassVar[str] = "CommentOnIssue"
    description: ClassVar[str] = "Comment on an existing GitHub issue."
    risk: ClassVar[RiskClass] = RiskClass.GITHUB
    args_model: ClassVar[type[BaseModel]] = CommentOnIssueArgs

    def __init__(
        self,
        tasks: Tasks,
        github: GitHubAuth,
        events: EventLog,
        http: httpx2.AsyncClient,
    ) -> None:
        self._tasks = tasks
        self._github = github
        self._events = events
        self._http = http

    async def preview(self, ctx: ToolContext, args: CommentOnIssueArgs) -> Preview:
        return Preview(kind="text", title=f"Comment on #{args.number}", body=args.body)

    async def run(self, ctx: ToolContext, args: CommentOnIssueArgs) -> ToolOutput:
        ref = await self._tasks.repo_ref(ctx.project_id)
        if ref is None:
            return ToolOutput(content="Project has no GitHub link.", is_error=True)

        token = await self._github.token()
        if not token:
            return ToolOutput(content="GitHub integration is not configured.", is_error=True)

        repo_full_name = f"{ref.owner}/{ref.repo}"
        try:
            issue = await get_issue(self._http, token, repo_full_name, args.number)
            comment = await comment_issue(self._http, token, repo_full_name, args.number, args.body)
        except DomainError as err:
            return ToolOutput(content=str(err), is_error=True)

        await self._events.record(
            ctx.project_id,
            source=ctx.agent,
            type="issue.commented",
            payload=issue.event_payload(),
            subject_id=str(args.number),
        )

        return ToolOutput(content=f"Commented on issue #{args.number}: {comment.html_url}")
