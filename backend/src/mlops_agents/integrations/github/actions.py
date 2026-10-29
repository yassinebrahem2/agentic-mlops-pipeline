"""GitHub Actions API calls (stage 3)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx2
from pydantic import BaseModel, TypeAdapter, ValidationError

from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.api import request, unreadable


class WorkflowRun(BaseModel):
    id: int
    name: str | None = None
    path: str
    status: str
    conclusion: str | None = None
    run_started_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    head_sha: str
    html_url: str


_RUN_LIST = TypeAdapter(list[WorkflowRun])


async def list_workflow_runs(
    http: httpx2.AsyncClient,
    token: str,
    repo: str,
    *,
    created_from: datetime,
    limit: int = 300,
) -> list[WorkflowRun]:
    """Lists workflow runs created at or after `created_from`, up to `limit`."""
    dt_utc = created_from if created_from.tzinfo is not None else created_from.replace(tzinfo=UTC)
    created_str = f">={dt_utc.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}"

    runs: list[WorkflowRun] = []
    page = 1
    while len(runs) < limit:
        params: dict[str, Any] = {
            "per_page": 100,
            "page": page,
            "created": created_str,
        }
        response = await request(
            http,
            "GET",
            f"/repos/{repo}/actions/runs",
            token,
            params=params,
            permission="actions: read",
        )
        if response.status_code != 200:
            raise DomainError(
                "github_error",
                f"GitHub answered with HTTP {response.status_code}.",
                status=502,
            )
        try:
            body = response.json()
            if not isinstance(body, dict) or "workflow_runs" not in body:
                raise unreadable()
            page_runs = _RUN_LIST.validate_python(body["workflow_runs"])
        except (ValidationError, Exception):
            raise unreadable() from None

        runs.extend(page_runs)
        if len(page_runs) < 100 or len(runs) >= limit:
            break
        page += 1

    return runs[:limit]
