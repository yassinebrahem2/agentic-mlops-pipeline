from __future__ import annotations

from collections.abc import Iterator

import httpx2
import pytest

from mlops_agents.errors import DomainError
from mlops_agents.integrations.github import api
from mlops_agents.integrations.github.merge import (
    CheckRun,
    MergeResult,
    PullMergeDetails,
    get_check_runs,
    get_last_issue_comment,
    get_pull_merge_state,
    merge_pull_request,
)
from mlops_agents.integrations.github.pulls import create_pull
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


async def test_get_check_runs(fake_github: RunningFakeGitHub) -> None:
    repo = "test-owner/test-repo"
    sha = "head123"
    fake_github.state.check_runs[(repo, sha)] = [
        {"name": "test-suite", "status": "completed", "conclusion": "success"},
        {"name": "lint", "status": "completed", "conclusion": "failure"},
        {"name": "build", "status": "in_progress", "conclusion": None},
    ]

    async with httpx2.AsyncClient() as http:
        runs = await get_check_runs(http, token="ghp_test", repo=repo, ref=sha)
        assert len(runs) == 3
        assert runs[0] == CheckRun(name="test-suite", status="completed", conclusion="success")
        assert runs[1] == CheckRun(name="lint", status="completed", conclusion="failure")
        assert runs[2] == CheckRun(name="build", status="in_progress", conclusion=None)


async def test_get_pull_merge_state(fake_github: RunningFakeGitHub) -> None:
    repo = "test-owner/test-repo"
    async with httpx2.AsyncClient() as http:
        pr = await create_pull(
            http,
            token="ghp_test",
            repo=repo,
            title="Clean PR",
            head="feature-1",
            base="main",
            body="PR description",
        )
        fake_github.state.prs[(repo, pr.number)]["head"] = {
            "sha": "sha_clean_pr",
            "ref": "feature-1",
        }
        fake_github.state.prs[(repo, pr.number)]["mergeable"] = True
        fake_github.state.prs[(repo, pr.number)]["mergeable_state"] = "clean"
        fake_github.state.prs[(repo, pr.number)]["changed_files"] = 2

        details = await get_pull_merge_state(http, token="ghp_test", repo=repo, number=pr.number)
        assert isinstance(details, PullMergeDetails)
        assert details.mergeable is True
        assert details.mergeable_state == "clean"
        assert details.head_sha == "sha_clean_pr"
        assert details.title == "Clean PR"
        assert details.changed_files == 2


async def test_merge_pull_request_success_and_conflict(fake_github: RunningFakeGitHub) -> None:
    repo = "test-owner/test-repo"
    async with httpx2.AsyncClient() as http:
        pr = await create_pull(
            http,
            token="ghp_test",
            repo=repo,
            title="PR to merge",
            head="feature-1",
            base="main",
        )
        fake_github.state.prs[(repo, pr.number)]["head"] = {
            "sha": "expected_sha_123",
            "ref": "feature-1",
        }

        # Attempting merge with wrong sha raises 409 DomainError
        with pytest.raises(DomainError) as exc_info:
            await merge_pull_request(
                http, token="ghp_test", repo=repo, number=pr.number, sha="outdated_sha"
            )
        assert exc_info.value.status == 409
        assert exc_info.value.code == "github_merge_conflict"

        # Attempting merge with correct sha succeeds
        res = await merge_pull_request(
            http, token="ghp_test", repo=repo, number=pr.number, sha="expected_sha_123"
        )
        assert isinstance(res, MergeResult)
        assert res.merged is True
        assert fake_github.state.prs[(repo, pr.number)]["merged"] is True


async def test_get_last_issue_comment(fake_github: RunningFakeGitHub) -> None:
    repo = "test-owner/test-repo"
    async with httpx2.AsyncClient() as http:
        pr = await create_pull(
            http,
            token="ghp_test",
            repo=repo,
            title="PR with comments",
            head="feature-1",
            base="main",
        )

        # No comments yet
        comment = await get_last_issue_comment(http, token="ghp_test", repo=repo, number=pr.number)
        assert comment is None

        # Add comments
        fake_github.state.comments[(repo, pr.number)] = [
            {"id": 1, "body": "First comment", "html_url": "url1"},
            {"id": 2, "body": "Closing comment with reason", "html_url": "url2"},
        ]

        last = await get_last_issue_comment(http, token="ghp_test", repo=repo, number=pr.number)
        assert last == "Closing comment with reason"
