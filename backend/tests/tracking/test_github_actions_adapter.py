"""Tests for GitHub Actions tracking adapter."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from mlops_agents.integrations.github import api
from mlops_agents.tracking.adapters.github_actions import (
    GITHUB_FIRST_SYNC_DAYS,
    GITHUB_OVERLAP,
    GithubActionsAdapter,
)
from mlops_agents.tracking.contract import (
    CiRunRow,
    TrackingQuery,
    TrackingSourceView,
)
from mlops_agents.tracking.service import TrackingService
from tests.fakes.fake_github import FakeGitHubState, RunningFakeGitHub, start_fake_github
from tests.tracking.conftest import TrackingEnv


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


def _make_source_view(
    source_id: str = "src_gh_1",
    repo: str = "octo/repo",
    workflows: list[str] | None = None,
    last_sync: datetime | None = None,
) -> TrackingSourceView:
    return TrackingSourceView(
        id=source_id,
        type="github_actions",
        settings={"repository": repo, "workflows": workflows or []},
        auth="none",
        credential_set=False,
        last_sync=last_sync,
        last_error=None,
    )


def _make_raw_run(
    run_id: int,
    *,
    status: str,
    conclusion: str | None = None,
    path: str = ".github/workflows/ci.yml",
    run_started_at: str | None = "2026-10-28T10:00:00Z",
    created_at: str = "2026-10-28T10:00:00Z",
    updated_at: str = "2026-10-28T10:05:00Z",
    head_sha: str = "commit_sha_123",
    html_url: str = "https://github.com/octo/repo/actions/runs/1",
) -> dict[str, object]:
    return {
        "id": run_id,
        "name": "CI",
        "path": path,
        "status": status,
        "conclusion": conclusion,
        "run_started_at": run_started_at,
        "created_at": created_at,
        "updated_at": updated_at,
        "head_sha": head_sha,
        "html_url": html_url,
    }


async def test_status_mapping_and_durations(fake_github: RunningFakeGitHub) -> None:
    cases = [
        # (status, conclusion, expected_status)
        ("queued", None, "running"),
        ("in_progress", None, "running"),
        ("waiting", None, "running"),
        ("requested", None, "running"),
        ("pending", None, "running"),
        ("completed", "success", "succeeded"),
        ("completed", "failure", "failed"),
        ("completed", "timed_out", "failed"),
        ("completed", "startup_failure", "failed"),
        ("completed", "cancelled", "cancelled"),
        ("completed", "skipped", "other"),
        ("completed", "neutral", "other"),
        ("completed", "action_required", "other"),
        ("completed", "stale", "other"),
        ("unknown_status", None, "other"),
    ]

    runs_data = [
        _make_raw_run(
            idx + 1,
            status=st,
            conclusion=concl,
            created_at="2026-10-28T10:00:00Z",
            run_started_at="2026-10-28T10:00:00Z",
            updated_at="2026-10-28T10:05:00Z",
        )
        for idx, (st, concl, _) in enumerate(cases)
    ]
    fake_github.state.workflow_runs["octo/repo"] = runs_data

    async with httpx2.AsyncClient() as http:
        adapter = GithubActionsAdapter(http)
        source = _make_source_view()
        result = await adapter.sync(source, credential="fake-token", since=None)

        assert result.error is None
        assert len(result.ci_runs) == len(cases)
        for idx, (_, _, expected_st) in enumerate(cases):
            row = result.ci_runs[idx]
            assert row.key == str(idx + 1)
            assert row.status == expected_st

        # Check duration: completed has 300.0s (5 min), running has None
        # case 0 is queued (running) -> duration_s is None
        assert result.ci_runs[0].duration_s is None
        # case 1 is in_progress (running) -> duration_s is None
        assert result.ci_runs[1].duration_s is None
        # case 5 is completed success -> duration_s is 300.0
        assert result.ci_runs[5].duration_s == 300.0


async def test_mapping_fields_and_run_started_fallback(fake_github: RunningFakeGitHub) -> None:
    fake_github.state.workflow_runs["octo/repo"] = [
        # completed run
        _make_raw_run(
            101,
            status="completed",
            conclusion="success",
            path=".github/workflows/ci.yml",
            run_started_at="2026-10-28T10:00:00Z",
            created_at="2026-10-28T09:59:00Z",
            updated_at="2026-10-28T10:10:00Z",
            head_sha="sha101",
            html_url="https://github.com/octo/repo/actions/runs/101",
        ),
        # failed run
        _make_raw_run(
            102,
            status="completed",
            conclusion="failure",
            path=".github/workflows/ci.yml",
            run_started_at="2026-10-28T10:00:00Z",
            created_at="2026-10-28T10:00:00Z",
            updated_at="2026-10-28T10:02:30Z",
            head_sha="sha102",
            html_url="https://github.com/octo/repo/actions/runs/102",
        ),
        # in-progress run with null run_started_at (fallback to created_at)
        _make_raw_run(
            103,
            status="in_progress",
            conclusion=None,
            path=".github/workflows/ci.yml",
            run_started_at=None,
            created_at="2026-10-28T11:00:00Z",
            updated_at="2026-10-28T11:05:00Z",
            head_sha="sha103",
            html_url="https://github.com/octo/repo/actions/runs/103",
        ),
    ]

    async with httpx2.AsyncClient() as http:
        adapter = GithubActionsAdapter(http)
        source = _make_source_view(source_id="src_gh_1")
        result = await adapter.sync(source, credential="fake-token", since=None)

        assert result.error is None
        assert len(result.ci_runs) == 3

        r1 = result.ci_runs[0]
        assert r1.source_id == "src_gh_1"
        assert r1.key == "101"
        assert r1.workflow == "ci.yml"
        assert r1.status == "succeeded"
        assert r1.start == datetime(2026, 10, 28, 10, 0, 0, tzinfo=UTC)
        assert r1.duration_s == 600.0
        assert r1.commit == "sha101"
        assert r1.url == "https://github.com/octo/repo/actions/runs/101"

        r2 = result.ci_runs[1]
        assert r2.key == "102"
        assert r2.status == "failed"
        assert r2.duration_s == 150.0

        r3 = result.ci_runs[2]
        assert r3.key == "103"
        assert r3.status == "running"
        assert r3.start == datetime(2026, 10, 28, 11, 0, 0, tzinfo=UTC)
        assert r3.duration_s is None


async def test_workflows_basename_filtering(fake_github: RunningFakeGitHub) -> None:
    fake_github.state.workflow_runs["octo/repo"] = [
        _make_raw_run(1, status="completed", conclusion="success", path=".github/workflows/ci.yml"),
        _make_raw_run(
            2, status="completed", conclusion="success", path=".github/workflows/deploy.yml"
        ),
        _make_raw_run(
            3, status="completed", conclusion="success", path=".github/workflows/release.yaml"
        ),
    ]

    async with httpx2.AsyncClient() as http:
        adapter = GithubActionsAdapter(http)

        # 1. workflows is empty -> keeps all
        src_all = _make_source_view(workflows=[])
        res_all = await adapter.sync(src_all, credential="token", since=None)
        assert [r.key for r in res_all.ci_runs] == ["1", "2", "3"]

        # 2. workflows specifies ["ci.yml"] -> keeps only ci.yml
        src_ci = _make_source_view(workflows=["ci.yml"])
        res_ci = await adapter.sync(src_ci, credential="token", since=None)
        assert [r.key for r in res_ci.ci_runs] == ["1"]

        # 3. workflows specifies ["deploy.yml", "release.yaml"]
        src_multi = _make_source_view(workflows=["deploy.yml", "release.yaml"])
        res_multi = await adapter.sync(src_multi, credential="token", since=None)
        assert [r.key for r in res_multi.ci_runs] == ["2", "3"]


async def test_first_sync_and_later_sync_windows(fake_github: RunningFakeGitHub) -> None:
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
    fake_github.state.workflow_runs["octo/repo"] = [
        _make_raw_run(
            1, status="completed", conclusion="success", created_at="2026-10-28T10:00:00Z"
        ),
    ]

    async with httpx2.AsyncClient() as http:
        adapter = GithubActionsAdapter(http, clock=lambda: now)
        source = _make_source_view()

        # First sync: since is None
        await adapter.sync(source, credential="fake-token", since=None)
        assert len(fake_github.state.requests) == 1
        req1 = fake_github.state.requests[0]
        expected_first = now - timedelta(days=GITHUB_FIRST_SYNC_DAYS)
        assert req1["params"]["created"] == f">={expected_first.strftime('%Y-%m-%dT%H:%M:%SZ')}"

        fake_github.state.requests.clear()

        # Later sync: since is set
        since_dt = datetime(2026, 10, 20, 10, 0, 0, tzinfo=UTC)
        await adapter.sync(source, credential="fake-token", since=since_dt)
        assert len(fake_github.state.requests) == 1
        req2 = fake_github.state.requests[0]
        expected_later = since_dt - GITHUB_OVERLAP
        assert req2["params"]["created"] == f">={expected_later.strftime('%Y-%m-%dT%H:%M:%SZ')}"


async def test_no_github_login_and_missing_permission(fake_github: RunningFakeGitHub) -> None:
    async with httpx2.AsyncClient() as http:
        adapter = GithubActionsAdapter(http)
        source = _make_source_view()

        # No credential -> Log in to GitHub to read CI runs
        res_no_login = await adapter.sync(source, credential=None, since=None)
        assert res_no_login.error == "Log in to GitHub to read CI runs"

        # Missing permission
        fake_github.state.simulate_permission_error["actions: read"] = 403
        res_no_perm = await adapter.sync(source, credential="fake-token", since=None)
        assert res_no_perm.error is not None
        assert "actions: read" in res_no_perm.error


async def test_sync_through_tracking_service_updates_in_place(
    fake_github: RunningFakeGitHub, tracking_env: TrackingEnv
) -> None:
    # 1. Setup run as in_progress
    fake_github.state.workflow_runs["octo/repo"] = [
        _make_raw_run(
            42,
            status="in_progress",
            conclusion=None,
            run_started_at="2026-10-28T10:00:00Z",
            created_at="2026-10-28T10:00:00Z",
            updated_at="2026-10-28T10:02:00Z",
        )
    ]

    async with httpx2.AsyncClient() as http:
        adapter = GithubActionsAdapter(http)
        service = TrackingService(
            databases=tracking_env.databases,
            keystore=tracking_env.keystore,
            secrets=tracking_env.secrets,
            events=tracking_env.events,
            adapters={"github_actions": adapter},
            github_token=tracking_env.get_github_token,
            clock=tracking_env.clock,
        )

        source_view = await service.create_source(
            tracking_env.project_id,
            type="github_actions",
            settings={"repository": "octo/repo"},
            auth="none",
        )

        # First sync
        await service.run_sync(tracking_env.project_id, source_view.id)

        page = await service.query(
            tracking_env.project_id, TrackingQuery(what="ci_runs", source_id=source_view.id)
        )
        assert page.total == 1
        run_row = page.rows[0]
        assert isinstance(run_row, CiRunRow)
        assert run_row.key == "42"
        assert run_row.status == "running"
        assert run_row.duration_s is None

        # 2. Update run to completed success
        fake_github.state.workflow_runs["octo/repo"] = [
            _make_raw_run(
                42,
                status="completed",
                conclusion="success",
                run_started_at="2026-10-28T10:00:00Z",
                created_at="2026-10-28T10:00:00Z",
                updated_at="2026-10-28T10:05:00Z",
            )
        ]

        # Second sync
        await service.run_sync(tracking_env.project_id, source_view.id)

        page2 = await service.query(
            tracking_env.project_id, TrackingQuery(what="ci_runs", source_id=source_view.id)
        )
        # Idempotent upsert: still total 1, updated in place
        assert page2.total == 1
        updated_row = page2.rows[0]
        assert updated_row.key == "42"
        assert updated_row.status == "succeeded"
        assert updated_row.duration_s == 300.0
