"""GitHub Actions workflow dispatch integration (stage 5 task 4)."""

from __future__ import annotations

import contextlib

import httpx2
from pydantic import BaseModel

from mlops_agents.errors import DomainError
from mlops_agents.integrations.github.api import request


class _ErrorPayload(BaseModel):
    message: str | None = None


async def dispatch_workflow(
    http: httpx2.AsyncClient,
    token: str,
    repo: str,
    workflow: str,
    ref: str,
    inputs: dict[str, str],
) -> None:
    """Dispatches a GitHub Actions workflow with inputs on the given ref."""
    response = await request(
        http,
        "POST",
        f"/repos/{repo}/actions/workflows/{workflow}/dispatches",
        token,
        json={"ref": ref, "inputs": inputs},
        permission="actions: write",
    )
    if response.status_code == 204:
        return
    if response.status_code == 422:
        detail = ""
        with contextlib.suppress(Exception):
            payload = _ErrorPayload.model_validate_json(response.text)
            if payload.message:
                detail = payload.message.strip()
        suffix = f": {detail}" if detail else "."
        raise DomainError(
            "workflow_dispatch_failed",
            f"Workflow '{workflow}' could not be dispatched{suffix}",
            status=422,
        )
    raise DomainError(
        "github_error",
        f"GitHub answered with HTTP {response.status_code}.",
        status=502,
    )
