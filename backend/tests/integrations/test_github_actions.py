"""Tests for GitHub Actions integration (client and fake route)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import httpx2
import pytest

from mlops_agents.errors import DomainError
from mlops_agents.integrations.github import api
from mlops_agents.integrations.github.actions import WorkflowRun, list_workflow_runs
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


def _sample_run(
    run_id: int, created_at: str, path: str = ".github/workflows/ci.yml"
) -> dict[str, object]:
    return {
        "id": run_id,
        "name": "CI",
        "path": path,
        "status": "completed",
        "conclusion": "success",
        "run_started_at": created_at,
        "created_at": created_at,
        "updated_at": created_at,
        "head_sha": f"sha_{run_id}",
        "html_url": f"https://github.com/octo/repo/actions/runs/{run_id}",
    }


async def test_fake_route_created_filtering_and_paging(fake_github: RunningFakeGitHub) -> None:
    state = fake_github.state
    # 5 runs created at different times
    state.workflow_runs["octo/repo"] = [
        _sample_run(1, "2026-10-01T10:00:00Z"),
        _sample_run(2, "2026-10-05T10:00:00Z"),
        _sample_run(3, "2026-10-10T10:00:00Z"),
        _sample_run(4, "2026-10-15T10:00:00Z"),
        _sample_run(5, "2026-10-20T10:00:00Z"),
    ]

    async with httpx2.AsyncClient() as http:
        # Paging with per_page=2, page=1
        res1 = await http.get(
            f"{fake_github.base_url}/repos/octo/repo/actions/runs",
            params={"per_page": 2, "page": 1},
        )
        assert res1.status_code == 200
        body1 = res1.json()
        assert body1["total_count"] == 5
        assert [r["id"] for r in body1["workflow_runs"]] == [1, 2]

        # Paging with per_page=2, page=2
        res2 = await http.get(
            f"{fake_github.base_url}/repos/octo/repo/actions/runs",
            params={"per_page": 2, "page": 2},
        )
        assert res2.status_code == 200
        body2 = res2.json()
        assert body2["total_count"] == 5
        assert [r["id"] for r in body2["workflow_runs"]] == [3, 4]

        # Filtering by created >= 2026-10-10T00:00:00Z
        res_filter = await http.get(
            f"{fake_github.base_url}/repos/octo/repo/actions/runs",
            params={"created": ">=2026-10-10T00:00:00Z"},
        )
        assert res_filter.status_code == 200
        body_filter = res_filter.json()
        assert body_filter["total_count"] == 3
        assert [r["id"] for r in body_filter["workflow_runs"]] == [3, 4, 5]


async def test_fake_route_auth_and_permissions(fake_github: RunningFakeGitHub) -> None:
    state = fake_github.state
    state.required_token = "secret-token"

    async with httpx2.AsyncClient() as http:
        # Without token -> 401
        res401 = await http.get(f"{fake_github.base_url}/repos/octo/repo/actions/runs")
        assert res401.status_code == 401

        # With wrong token -> 401
        res_bad = await http.get(
            f"{fake_github.base_url}/repos/octo/repo/actions/runs",
            headers={"Authorization": "Bearer wrong"},
        )
        assert res_bad.status_code == 401

        # With correct token -> 200
        res200 = await http.get(
            f"{fake_github.base_url}/repos/octo/repo/actions/runs",
            headers={"Authorization": "Bearer secret-token"},
        )
        assert res200.status_code == 200

        # Simulate permission error
        state.simulate_permission_error["actions: read"] = 403
        res403 = await http.get(
            f"{fake_github.base_url}/repos/octo/repo/actions/runs",
            headers={"Authorization": "Bearer secret-token"},
        )
        assert res403.status_code == 403


async def test_list_workflow_runs_paging_and_limit(fake_github: RunningFakeGitHub) -> None:
    state = fake_github.state
    # Populate 150 runs to force paging across page 1 (100) and page 2 (50)
    state.workflow_runs["octo/repo"] = [
        _sample_run(i, f"2026-10-{(i % 25) + 1:02d}T10:00:00Z") for i in range(1, 151)
    ]

    async with httpx2.AsyncClient() as http:
        # All runs up to limit 200
        runs = await list_workflow_runs(
            http,
            token="any-token",
            repo="octo/repo",
            created_from=datetime(2026, 10, 1, tzinfo=UTC),
            limit=200,
        )
        assert len(runs) == 150
        assert all(isinstance(r, WorkflowRun) for r in runs)
        assert runs[0].id == 1
        assert runs[-1].id == 150

        # Respect limit parameter
        runs_limit = await list_workflow_runs(
            http,
            token="any-token",
            repo="octo/repo",
            created_from=datetime(2026, 10, 1, tzinfo=UTC),
            limit=5,
        )
        assert len(runs_limit) == 5


async def test_list_workflow_runs_errors() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        auth = request.headers.get("authorization")
        if auth == "Bearer bad_token":
            return httpx2.Response(401, json={"message": "Bad credentials"})
        if auth == "Bearer rate_limited":
            return httpx2.Response(
                429, headers={"retry-after": "60"}, json={"message": "rate limit"}
            )
        if auth == "Bearer no_perm":
            return httpx2.Response(403, json={"message": "Resource not accessible"})
        if auth == "Bearer unreadable_json":
            return httpx2.Response(200, content=b"invalid json")
        if auth == "Bearer bad_schema":
            return httpx2.Response(200, json={"workflow_runs": [{"id": "not an int"}]})
        return httpx2.Response(200, json={"workflow_runs": []})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        created_from = datetime(2026, 10, 1, tzinfo=UTC)

        # 401 -> github_token_rejected
        with pytest.raises(DomainError) as exc401:
            await list_workflow_runs(http, "bad_token", "octo/repo", created_from=created_from)
        assert exc401.value.code == "github_token_rejected"

        # 429 -> github_rate_limited
        with pytest.raises(DomainError) as exc429:
            await list_workflow_runs(http, "rate_limited", "octo/repo", created_from=created_from)
        assert exc429.value.code == "github_rate_limited"

        # 403 -> github_permission_missing
        with pytest.raises(DomainError) as exc403:
            await list_workflow_runs(http, "no_perm", "octo/repo", created_from=created_from)
        assert exc403.value.code == "github_permission_missing"
        assert "actions: read" in exc403.value.message

        # unreadable body -> github_error
        with pytest.raises(DomainError) as exc_unreadable:
            await list_workflow_runs(
                http, "unreadable_json", "octo/repo", created_from=created_from
            )
        assert exc_unreadable.value.code == "github_error"

        # bad schema -> github_error
        with pytest.raises(DomainError) as exc_bad_schema:
            await list_workflow_runs(http, "bad_schema", "octo/repo", created_from=created_from)
        assert exc_bad_schema.value.code == "github_error"
