from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest

from mlops_agents.agents.approvals.authority import ActionCall, Authority
from mlops_agents.agents.policy.actions import decide_action
from mlops_agents.agents.pr_sync import PrSyncObserver
from mlops_agents.agents.runtime.contract import TaskView
from mlops_agents.agents.tasks.contract import Tasks
from mlops_agents.agents.tools.base import RiskClass, ToolContext, ToolFailure
from mlops_agents.agents.tools.merge import MergePullRequestArgs, MergePullRequestTool
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog
from mlops_agents.integrations.github import api
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.integrations.github.pulls import create_pull
from mlops_agents.promotion.contract import ProtectionSurfaces
from mlops_agents.workspace.refs import RepoRef
from tests.fakes.fake_github import FakeGitHubState, RunningFakeGitHub, start_fake_github
from tests.fakes.fake_tasks import FakeTasks


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


class DummyFiles:
    root = Path()

    async def tracked(self) -> list[str]:
        return []

    async def under(self, prefix: str) -> list[str]:
        return []

    async def resolve_file(self, user_path: str) -> str:
        return user_path

    async def resolve_dir(self, user_path: str | None) -> str:
        return user_path or "."

    def invalidate(self) -> None:
        pass


class FakeGitHubAuth:
    def __init__(self, value: str = "test") -> None:
        self.value = value

    async def token(self) -> str | None:
        return self.value


class FakeEventLog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def record(
        self,
        project_id: str,
        source: str,
        type: str,
        payload: dict[str, Any],
        subject_id: str | None = None,
    ) -> None:
        self.events.append(
            {
                "project_id": project_id,
                "source": source,
                "type": type,
                "payload": payload,
                "subject_id": subject_id,
            }
        )


class FakePrSync:
    def __init__(self) -> None:
        self.synced: list[str] = []

    async def sync_now(self, project_id: str) -> None:
        self.synced.append(project_id)


class FakeProtectionSurfaces:
    def __init__(
        self, touched_paths: list[str] | None = None, exc: Exception | None = None
    ) -> None:
        self.touched_paths = touched_paths or []
        self.exc = exc

    async def paths(self, project_id: str) -> list[str]:
        return list(self.touched_paths)

    async def touches(self, project_id: str, pr_number: int) -> list[str]:
        if self.exc is not None:
            raise self.exc
        return list(self.touched_paths)


class ToolTasks(FakeTasks):
    def __init__(
        self, open_tasks: list[TaskView], owner: str = "test-owner", repo: str = "test-repo"
    ) -> None:
        super().__init__(open_tasks)
        self._ref = RepoRef(owner, repo)

    async def repo_ref(self, project_id: str) -> RepoRef | None:
        return self._ref


def _make_tool(
    *,
    tasks: ToolTasks | None = None,
    github: FakeGitHubAuth | None = None,
    http: httpx2.AsyncClient,
    events: FakeEventLog | None = None,
    surfaces: FakeProtectionSurfaces | None = None,
    pr_sync: FakePrSync | None = None,
) -> MergePullRequestTool:
    return MergePullRequestTool(
        tasks=cast(Tasks, tasks or ToolTasks([])),
        github=cast(GitHubAuth, github or FakeGitHubAuth()),
        http=http,
        events=cast(EventLog, events or FakeEventLog()),
        surfaces=cast(ProtectionSurfaces, surfaces or FakeProtectionSurfaces([])),
        pr_sync=cast(PrSyncObserver, pr_sync or FakePrSync()),
    )


def _make_context(agent: str = "coder", project_id: str = "proj_1") -> ToolContext:
    return ToolContext(
        project_id=project_id,
        agent=agent,
        session_id="ses_1",
        turn_id="turn_1",
        files=DummyFiles(),
        task=None,
        tool_call_id="call_merge_1",
    )


async def test_merge_clean_own_pr_records_event_and_syncs(fake_github: RunningFakeGitHub) -> None:
    repo = "test-owner/test-repo"
    head_sha = "sha_clean_head"
    async with httpx2.AsyncClient() as http:
        pr = await create_pull(http, "ghp_test", repo, title="Clean PR", head="feat", base="main")
        fake_github.state.prs[(repo, pr.number)]["head"] = {"sha": head_sha, "ref": "feat"}
        fake_github.state.prs[(repo, pr.number)]["mergeable"] = True
        fake_github.state.prs[(repo, pr.number)]["mergeable_state"] = "clean"
        fake_github.state.check_runs[(repo, head_sha)] = [
            {"name": "test", "status": "completed", "conclusion": "success"}
        ]

        task = TaskView(
            id="tsk_101",
            agent="coder",
            title="Clean PR",
            branch="feat",
            status="in_review",
            review_kind="github",
            pr_number=pr.number,
            pr_url=f"https://github.com/{repo}/pull/{pr.number}",
        )
        tasks = ToolTasks([task])
        events = FakeEventLog()
        pr_sync = FakePrSync()
        surfaces = FakeProtectionSurfaces([])

        tool = _make_tool(
            tasks=tasks,
            http=http,
            events=events,
            surfaces=surfaces,
            pr_sync=pr_sync,
        )

        ctx = _make_context(agent="coder")
        args = MergePullRequestArgs(pr_number=pr.number)

        # Preview passes
        preview = await tool.preview(ctx, args)
        assert "Merge PR" in preview.title

        # Run merges
        output = await tool.run(ctx, args)
        assert output.is_error is False
        assert f"Pull request #{pr.number} merged successfully." in output.content

        # PR is merged in fake GitHub with correct sha
        assert fake_github.state.prs[(repo, pr.number)]["merged"] is True

        # pr.merged_by_agent event was recorded
        merge_events = [e for e in events.events if e["type"] == "pr.merged_by_agent"]
        assert len(merge_events) == 1
        assert merge_events[0]["subject_id"] == "tsk_101"
        assert merge_events[0]["source"] == "coder"
        assert merge_events[0]["payload"] == {"pr_number": pr.number, "sha": head_sha}

        # sync_now was called
        assert pr_sync.synced == ["proj_1"]


async def test_merge_moved_head_gets_409_and_records_nothing(
    fake_github: RunningFakeGitHub,
) -> None:
    repo = "test-owner/test-repo"
    head_sha = "sha_orig"
    async with httpx2.AsyncClient() as http:
        pr = await create_pull(http, "ghp_test", repo, title="Clean PR", head="feat", base="main")
        fake_github.state.prs[(repo, pr.number)]["head"] = {"sha": head_sha, "ref": "feat"}
        fake_github.state.prs[(repo, pr.number)]["mergeable"] = True
        fake_github.state.prs[(repo, pr.number)]["mergeable_state"] = "clean"
        fake_github.state.check_runs[(repo, head_sha)] = [
            {"name": "test", "status": "completed", "conclusion": "success"}
        ]

        task = TaskView(
            id="tsk_101",
            agent="coder",
            title="Clean PR",
            branch="feat",
            status="in_review",
            review_kind="github",
            pr_number=pr.number,
            pr_url=f"https://github.com/{repo}/pull/{pr.number}",
        )
        tasks = ToolTasks([task])
        events = FakeEventLog()
        pr_sync = FakePrSync()
        surfaces = FakeProtectionSurfaces([])

        tool = _make_tool(
            tasks=tasks,
            http=http,
            events=events,
            surfaces=surfaces,
            pr_sync=pr_sync,
        )

        ctx = _make_context(agent="coder")
        args = MergePullRequestArgs(pr_number=pr.number)

        # Simulate head moved on remote right before merge call
        original_check = tool.check_merge

        async def _check_and_move_head(c: ToolContext, p: int) -> tuple[TaskView, str]:
            res = await original_check(c, p)
            # Head moved on GitHub!
            fake_github.state.prs[(repo, pr.number)]["head"] = {"sha": "sha_moved", "ref": "feat"}
            return res

        tool.check_merge = _check_and_move_head  # type: ignore[assignment]

        with pytest.raises(DomainError) as exc_info:
            await tool.run(ctx, args)
        assert exc_info.value.status == 409

        # Nothing is recorded, sync_now not called
        assert len(events.events) == 0
        assert len(pr_sync.synced) == 0


async def test_merge_refusals(fake_github: RunningFakeGitHub) -> None:
    repo = "test-owner/test-repo"
    head_sha = "sha_check"
    async with httpx2.AsyncClient() as http:
        pr = await create_pull(http, "ghp_test", repo, title="Refusal PR", head="feat", base="main")
        fake_github.state.prs[(repo, pr.number)]["head"] = {"sha": head_sha, "ref": "feat"}
        fake_github.state.prs[(repo, pr.number)]["mergeable"] = True
        fake_github.state.prs[(repo, pr.number)]["mergeable_state"] = "clean"
        fake_github.state.check_runs[(repo, head_sha)] = [
            {"name": "test", "status": "completed", "conclusion": "success"}
        ]

        ctx = _make_context(agent="coder")
        args = MergePullRequestArgs(pr_number=pr.number)

        # 1. Refuses another agent's PR
        other_agent_task = TaskView(
            id="tsk_other",
            agent="lead",
            title="Refusal PR",
            branch="feat",
            status="in_review",
            review_kind="github",
            pr_number=pr.number,
            pr_url=f"https://github.com/{repo}/pull/{pr.number}",
        )
        tool = _make_tool(
            tasks=ToolTasks([other_agent_task]),
            http=http,
        )
        with pytest.raises(ToolFailure) as err:
            await tool.run(ctx, args)
        assert "not owned" in str(err.value).lower() or "not an open pr" in str(err.value).lower()

        # 2. Refuses local-review task
        local_task = TaskView(
            id="tsk_local",
            agent="coder",
            title="Local PR",
            branch="feat",
            status="in_review",
            review_kind="local",
            pr_number=pr.number,
            pr_url=None,
        )
        tool = _make_tool(
            tasks=ToolTasks([local_task]),
            http=http,
        )
        with pytest.raises(ToolFailure) as err:
            await tool.run(ctx, args)
        assert "local" in str(err.value).lower()

        # Own valid task for remaining tests
        own_task = TaskView(
            id="tsk_own",
            agent="coder",
            title="Valid task",
            branch="feat",
            status="in_review",
            review_kind="github",
            pr_number=pr.number,
            pr_url=f"https://github.com/{repo}/pull/{pr.number}",
        )

        # 3. Refuses mergeable_state blocked or dirty
        for bad_state in ("blocked", "dirty"):
            fake_github.state.prs[(repo, pr.number)]["mergeable_state"] = bad_state
            tool = _make_tool(
                tasks=ToolTasks([own_task]),
                http=http,
            )
            with pytest.raises(ToolFailure) as err:
                await tool.run(ctx, args)
            assert bad_state in str(err.value)

        fake_github.state.prs[(repo, pr.number)]["mergeable_state"] = "clean"

        # 4. Refuses no check runs
        fake_github.state.check_runs[(repo, head_sha)] = []
        tool = _make_tool(
            tasks=ToolTasks([own_task]),
            http=http,
        )
        with pytest.raises(ToolFailure) as err:
            await tool.run(ctx, args)
        assert "check runs" in str(err.value).lower()

        # 5. Refuses failed and pending check runs
        fake_github.state.check_runs[(repo, head_sha)] = [
            {"name": "test", "status": "completed", "conclusion": "success"},
            {"name": "lint", "status": "completed", "conclusion": "failure"},
            {"name": "build", "status": "in_progress", "conclusion": None},
        ]
        tool = _make_tool(
            tasks=ToolTasks([own_task]),
            http=http,
        )
        with pytest.raises(ToolFailure) as err:
            await tool.run(ctx, args)
        assert "failed or pending" in str(err.value).lower()

        # Reset checks to clean passing
        fake_github.state.check_runs[(repo, head_sha)] = [
            {"name": "test", "status": "completed", "conclusion": "success"}
        ]

        # 6. Refuses protected surfaces touched (.github/workflows/ci.yml, CODEOWNERS, docs/CODEOWNERS, deploy hook)
        for surface_path in (
            ".github/workflows/ci.yml",
            "CODEOWNERS",
            "docs/CODEOWNERS",
            "deploy/serve.py",
        ):
            tool = _make_tool(
                tasks=ToolTasks([own_task]),
                http=http,
                surfaces=FakeProtectionSurfaces([surface_path]),
            )
            with pytest.raises(ToolFailure) as err:
                await tool.run(ctx, args)
            assert surface_path in str(err.value)

        # 7. Refuses when files exceed what GitHub returns
        tool = _make_tool(
            tasks=ToolTasks([own_task]),
            http=http,
            surfaces=FakeProtectionSurfaces(
                exc=DomainError(
                    "surface_check_failed", "PR changed files exceed files returned by GitHub"
                )
            ),
        )
        with pytest.raises(ToolFailure) as err:
            await tool.run(ctx, args)
        assert "exceed" in str(err.value).lower()


async def test_merge_pr_action_call_and_gate_decisions() -> None:
    tool = _make_tool(http=httpx2.AsyncClient())
    assert tool.name == "MergePullRequest"
    assert tool.risk == RiskClass.DEPLOY

    ctx = _make_context(agent="coder", project_id="proj_1")
    args = MergePullRequestArgs(pr_number=42)

    action = await tool.action_call(args, ctx)
    assert action is not None
    assert action == ActionCall(project_id="proj_1", agent="coder", kind="merge_pr")

    auth = Authority(basis="none", reason="none")
    # Gate policy checks for merge_pr:
    # denied at suggest
    assert decide_action("suggest", action, auth) == "deny"
    # asked at ask
    assert decide_action("ask", action, auth) == "ask"
    # asked at autonomous
    assert decide_action("autonomous", action, auth) == "ask"
    # allowed at bypass
    assert decide_action("bypass", action, auth) == "allow"
