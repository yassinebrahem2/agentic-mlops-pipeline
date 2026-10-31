"""GitHub pull request merge and check runs API calls (I38)."""

from __future__ import annotations

from typing import cast

import httpx2
from pydantic import BaseModel, ConfigDict, ValidationError

from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.api import (
    PERMISSION_ISSUES_WRITE,
    PERMISSION_PULL_REQUESTS_WRITE,
    permission_missing,
    request,
    unreadable,
)


class CheckRun(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    status: str
    conclusion: str | None = None


class PullMergeDetails(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mergeable: bool | None
    mergeable_state: str | None
    head_sha: str
    title: str
    changed_files: int | None = None


class MergeResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sha: str
    merged: bool
    message: str


async def get_check_runs(
    http: httpx2.AsyncClient,
    token: str,
    repo: str,
    ref: str,
) -> list[CheckRun]:
    response = await request(
        http,
        "GET",
        f"/repos/{repo}/commits/{ref}/check-runs",
        token,
    )
    if response.status_code != 200:
        raise DomainError(
            "github_error", f"GitHub answered with HTTP {response.status_code}.", status=502
        )
    try:
        data: object = response.json()
        if not isinstance(data, dict):
            raise unreadable()
        data_dict = cast(dict[str, object], data)
        raw_runs = data_dict.get("check_runs")
        if not isinstance(raw_runs, list):
            raw_runs = []
        return [CheckRun.model_validate(r) for r in cast(list[object], raw_runs)]
    except (ValidationError, ValueError):
        raise unreadable() from None


async def get_pull_merge_state(
    http: httpx2.AsyncClient,
    token: str,
    repo: str,
    number: int,
) -> PullMergeDetails:
    response = await request(
        http,
        "GET",
        f"/repos/{repo}/pulls/{number}",
        token,
    )
    if response.status_code == 404:
        raise DomainError(
            "github_pr_not_found", f"Pull request #{number} no longer exists.", status=404
        )
    if response.status_code == 403:
        raise permission_missing(PERMISSION_PULL_REQUESTS_WRITE)
    if response.status_code != 200:
        raise DomainError(
            "github_error", f"GitHub answered with HTTP {response.status_code}.", status=502
        )
    try:
        data: object = response.json()
        if not isinstance(data, dict):
            raise unreadable()
        data_dict = cast(dict[str, object], data)
        head = data_dict.get("head")
        head_sha: str = ""
        if isinstance(head, dict):
            head_dict = cast(dict[str, object], head)
            head_sha = str(head_dict.get("sha") or "")
        elif head is not None:
            head_sha = str(head)
        return PullMergeDetails(
            mergeable=cast(bool | None, data_dict.get("mergeable")),
            mergeable_state=cast(str | None, data_dict.get("mergeable_state")),
            head_sha=head_sha,
            title=str(data_dict.get("title") or ""),
            changed_files=cast(int | None, data_dict.get("changed_files")),
        )
    except (ValidationError, ValueError):
        raise unreadable() from None


async def merge_pull_request(
    http: httpx2.AsyncClient,
    token: str,
    repo: str,
    number: int,
    sha: str,
) -> MergeResult:
    response = await request(
        http,
        "PUT",
        f"/repos/{repo}/pulls/{number}/merge",
        token,
        json={"sha": sha},
        permission=PERMISSION_PULL_REQUESTS_WRITE,
    )
    if response.status_code == 404:
        raise DomainError(
            "github_pr_not_found", f"Pull request #{number} no longer exists.", status=404
        )
    if response.status_code == 409:
        raise DomainError(
            "github_merge_conflict",
            "Head commit moved or merge conflict",
            status=409,
        )
    if response.status_code != 200:
        raise DomainError(
            "github_error", f"GitHub answered with HTTP {response.status_code}.", status=502
        )
    try:
        return MergeResult.model_validate(response.json())
    except (ValidationError, ValueError):
        raise unreadable() from None


async def get_last_issue_comment(
    http: httpx2.AsyncClient,
    token: str,
    repo: str,
    number: int,
) -> str | None:
    response = await request(
        http,
        "GET",
        f"/repos/{repo}/issues/{number}/comments",
        token,
        permission=PERMISSION_ISSUES_WRITE,
    )
    if response.status_code == 404:
        raise DomainError(
            "github_pr_not_found", f"Issue or PR #{number} no longer exists.", status=404
        )
    if response.status_code != 200:
        raise DomainError(
            "github_error", f"GitHub answered with HTTP {response.status_code}.", status=502
        )
    try:
        comments: object = response.json()
        if not isinstance(comments, list) or not comments:
            return None
        comments_list = cast(list[object], comments)
        last = comments_list[-1]
        if isinstance(last, dict):
            last_dict = cast(dict[str, object], last)
            body = last_dict.get("body")
            return str(body) if body is not None else None
        return None
    except (ValidationError, ValueError):
        raise unreadable() from None
