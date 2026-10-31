"""GitHub pull request merge and check runs API calls (I38)."""

from __future__ import annotations

from typing import cast

import httpx2
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

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
    *,
    per_page: int = 100,
) -> list[CheckRun]:
    runs: list[CheckRun] = []
    page = 1
    while True:
        response = await request(
            http,
            "GET",
            f"/repos/{repo}/commits/{ref}/check-runs",
            token,
            params={"per_page": per_page, "page": page},
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
            page_runs = [CheckRun.model_validate(r) for r in cast(list[object], raw_runs)]
            runs.extend(page_runs)
            if len(page_runs) < per_page:
                break
            page += 1
        except (ValidationError, ValueError):
            raise unreadable() from None
    return runs


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


class _Label(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str


class _OpenPull(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: int
    labels: list[_Label] = []


_OPEN_PULLS = TypeAdapter(list[_OpenPull])


async def get_open_pull_labels(
    http: httpx2.AsyncClient,
    token: str,
    repo: str,
) -> dict[int, list[str]]:
    """The label names of the repository's open pull requests, by PR number, in one call.

    One page of 100: labels of open pull requests beyond the first 100 are not read.
    """
    response = await request(
        http,
        "GET",
        f"/repos/{repo}/pulls",
        token,
        params={"state": "open", "per_page": 100},
        permission=PERMISSION_PULL_REQUESTS_WRITE,
    )
    if response.status_code != 200:
        raise DomainError(
            "github_error", f"GitHub answered with HTTP {response.status_code}.", status=502
        )
    try:
        pulls = _OPEN_PULLS.validate_python(response.json())
    except (ValidationError, ValueError):
        raise unreadable() from None
    return {pull.number: [label.name for label in pull.labels] for pull in pulls}
