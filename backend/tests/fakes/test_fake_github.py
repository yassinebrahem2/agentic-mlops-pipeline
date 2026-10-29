from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import httpx2
import pytest

if TYPE_CHECKING:
    from fake_github import (
        FakeGitHubState,
        RunningFakeGitHub,
        create_fake_github_app,
        start_fake_github,
    )
else:
    from tests.fakes.fake_github import (
        FakeGitHubState,
        RunningFakeGitHub,
        create_fake_github_app,
        start_fake_github,
    )


@pytest.fixture
def fake_server() -> Iterator[RunningFakeGitHub]:
    state = FakeGitHubState()
    running = start_fake_github(state)
    try:
        yield running
    finally:
        running.stop()


async def test_fake_github_lifecycle(fake_server: RunningFakeGitHub) -> None:
    # Direct app test with ASGITransport
    app = create_fake_github_app(fake_server.state)
    assert app.title == "Fake GitHub"

    async with httpx2.AsyncClient(base_url=fake_server.base_url) as client:
        # Check health/probe
        resp = await client.get("/user")
        assert resp.status_code == 200
        assert resp.json()["login"] == "fake-user"


async def test_fake_github_pulls(fake_server: RunningFakeGitHub) -> None:
    async with httpx2.AsyncClient(base_url=fake_server.base_url) as client:
        # Create PR
        create_resp = await client.post(
            "/repos/test-org/test-repo/pulls",
            json={"title": "Test PR", "head": "feature", "base": "main", "body": "PR description"},
        )
        assert create_resp.status_code == 201
        pr = create_resp.json()
        assert pr["number"] == 1
        assert pr["title"] == "Test PR"
        assert pr["state"] == "open"
        assert pr["merged"] is False

        # Get PR
        get_resp = await client.get("/repos/test-org/test-repo/pulls/1")
        assert get_resp.status_code == 200
        assert get_resp.json()["number"] == 1

        # Update PR
        patch_resp = await client.patch(
            "/repos/test-org/test-repo/pulls/1",
            json={"title": "Updated PR"},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["title"] == "Updated PR"

        # Merge PR via state helper
        fake_server.state.merge_pull("test-org/test-repo", 1)
        merged_resp = await client.get("/repos/test-org/test-repo/pulls/1")
        assert merged_resp.json()["merged"] is True
        assert merged_resp.json()["state"] == "closed"


async def test_fake_github_issues_and_comments(fake_server: RunningFakeGitHub) -> None:
    async with httpx2.AsyncClient(base_url=fake_server.base_url) as client:
        # Create issue
        create_resp = await client.post(
            "/repos/test-org/test-repo/issues",
            json={"title": "Issue 1", "body": "Issue body", "labels": ["bug"]},
        )
        assert create_resp.status_code == 201
        issue = create_resp.json()
        assert issue["number"] == 1
        assert issue["title"] == "Issue 1"

        # Add comment
        comment_resp = await client.post(
            "/repos/test-org/test-repo/issues/1/comments",
            json={"body": "Nice comment"},
        )
        assert comment_resp.status_code == 201
        comment = comment_resp.json()
        assert comment["id"] == 1
        assert comment["body"] == "Nice comment"

        # Search issues
        search_resp = await client.get(
            "/search/issues",
            params={"q": "repo:test-org/test-repo is:issue is:open Issue"},
        )
        assert search_resp.status_code == 200
        items = search_resp.json()["items"]
        assert len(items) == 1
        assert items[0]["number"] == 1


async def test_fake_github_labels(fake_server: RunningFakeGitHub) -> None:
    async with httpx2.AsyncClient(base_url=fake_server.base_url) as client:
        # Create label
        resp1 = await client.post(
            "/repos/test-org/test-repo/labels", json={"name": "priority:high"}
        )
        assert resp1.status_code == 201

        # Duplicate label returns 422
        resp2 = await client.post(
            "/repos/test-org/test-repo/labels", json={"name": "priority:high"}
        )
        assert resp2.status_code == 422


async def test_fake_github_permission_simulation(fake_server: RunningFakeGitHub) -> None:
    fake_server.state.simulate_permission_error["pull requests: write"] = 403
    async with httpx2.AsyncClient(base_url=fake_server.base_url) as client:
        resp = await client.post(
            "/repos/test-org/test-repo/pulls",
            json={"title": "PR", "head": "feat", "base": "main"},
        )
        assert resp.status_code == 403


async def test_fake_github_pull_files_and_merged_at(fake_server: RunningFakeGitHub) -> None:
    repo = "test-org/test-repo"
    async with httpx2.AsyncClient(base_url=fake_server.base_url) as client:
        # 1. Create PR
        create_resp = await client.post(
            f"/repos/{repo}/pulls",
            json={"title": "Files PR", "head": "f", "base": "main"},
        )
        assert create_resp.status_code == 201
        pr = create_resp.json()
        pr_number = pr["number"]
        assert pr["merged_at"] is None
        assert pr["changed_files"] == 0

        # Set files on fake github (including a rename)
        fake_server.state.pr_files[(repo, pr_number)] = [
            {"filename": "renamed.py", "previous_filename": "old.py"},
            {"filename": "file2.py", "previous_filename": None},
            {"filename": "file3.py", "previous_filename": None},
        ]

        # Get PR now reflects changed_files
        get_resp = await client.get(f"/repos/{repo}/pulls/{pr_number}")
        assert get_resp.json()["changed_files"] == 3

        # Paged files request: page 1 (per_page=2)
        f_resp1 = await client.get(f"/repos/{repo}/pulls/{pr_number}/files?per_page=2&page=1")
        assert f_resp1.status_code == 200
        page1 = f_resp1.json()
        assert len(page1) == 2
        assert page1[0]["filename"] == "renamed.py"
        assert page1[0]["previous_filename"] == "old.py"
        assert page1[1]["filename"] == "file2.py"

        # page 2
        f_resp2 = await client.get(f"/repos/{repo}/pulls/{pr_number}/files?per_page=2&page=2")
        assert f_resp2.status_code == 200
        page2 = f_resp2.json()
        assert len(page2) == 1
        assert page2[0]["filename"] == "file3.py"

        # Merge via route
        merge_resp = await client.put(f"/repos/{repo}/pulls/{pr_number}/merge")
        assert merge_resp.status_code == 200
        after_merge = (await client.get(f"/repos/{repo}/pulls/{pr_number}")).json()
        assert after_merge["merged"] is True
        assert after_merge["merged_at"] is not None
