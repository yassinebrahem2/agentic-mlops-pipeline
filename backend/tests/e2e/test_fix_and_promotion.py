# pyright: reportPrivateUsage=false
"""The stage 5 end-to-end story (S5 §8, Task 2): a confirmed decay, the fix, the promotion.

Parametrised over the two promotion modes, one project each. Both have a tracker-alias
production signal, no training policy (the run asks, and the test approves it as the person),
and a fake MLflow that holds the champion before the story and plays the team's pipeline after
the dispatch.

Story progression:
1. A label-based check of Model Performance fires: its incident wakes Model Performance, which
   calls `RequestFix`. Retraining is woken with `fix_request`, records `retrain` with the
   reasons for the other options, and its `RunHook` on the train hook asks. After the approval
   the guards pass and the fake GitHub receives one dispatch on the default branch.
2. The team's pipeline tags its run with the dispatched request ID: the run links the request
   (`training.linked`). Retraining, woken again, reads the evaluation and saves the report.
3. `person`: the promotion proposal is approved, which re-validates and runs the deploy hook
   (basis `approval`). `team_gate`: no deploy hook runs; the gate's decision is read from the
   evaluation record and the first on-call person is notified.
4. The production alias moves: Deployment & Rollback is woken once with `post_deploy` for that
   move and registers a follow-up. That the whole story wakes it exactly once is a separate
   test that fails until bug #649 is fixed: the champion's first appearance (none to version 1)
   is recorded after the trigger's cursor was seeded, so it wakes it too.
5. Messaged by the test, Deployment & Rollback opens a pull request improving the evaluation
   step against the fake GitHub (its own test: the pull request needs the origin served from a
   bare repository, which the workflow dispatch of scene 1 cannot share).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncGenerator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from mlops_agents.agents.approvals.contract import ApprovalView
from mlops_agents.agents.tools.names import RUN_HOOK
from mlops_agents.api.services import Services
from mlops_agents.connections.contract import LOCAL_FOLDER_KIND
from mlops_agents.monitoring.hooks.contract import HookView
from mlops_agents.monitoring.incidents.contract import IncidentView
from mlops_agents.monitoring.settings import set_monitoring_settings
from mlops_agents.promotion.constants import REQUIRED_REPORT_FIELDS
from mlops_agents.promotion.contract import ReportView
from mlops_agents.proposals.contract import PROMOTION_KIND
from mlops_agents.proposals.payloads import HandoffPayload
from mlops_agents.storage.tables import agent_wakes, notification_log
from mlops_agents.training.contract import FixRequestView
from tests.e2e.fixture_project import (
    CONNECTION_NAME,
    GITHUB_REPOSITORY,
    LABEL_MATURITY_DAYS,
    MODEL_NAME,
    PRODUCTION_ALIAS,
    TRAIN_WORKFLOW,
    PromotionMode,
    create_fix_repo,
    fix_story_handoff_payload,
    git_instead_of,
    label_check_record,
    make_bare_origin,
    monitoring_settings_payload,
    write_fixture_data_folder,
    write_results_record,
)
from tests.e2e.harness import (
    E2EHarness,
    SettableClock,
    create_e2e_harness,
    run_due,
    wait_for_agent_idle,
)
from tests.e2e.seeded_mlflow import (
    CHALLENGER_RUN,
    CHALLENGER_VERSION,
    CHAMPION_VERSION,
    play_pipeline,
    seed_champion,
)
from tests.fakes.fake_mlflow import FakeMlflowState, start_fake_mlflow
from tests.fakes.monitoring_script import (
    EVALUATION_FIELD,
    EVALUATION_FILE,
    EVALUATION_PR_MESSAGE,
    EVALUATION_PR_TITLE,
    FIRST_ON_CALL,
    NEW_VERSION_LINE,
    OPEN_PULL_REQUEST,
    POST_DEPLOY_FOLLOW_UP,
    START_TASK,
    TEAM_GATE_TITLE,
    WRITE,
    FixStoryIds,
    reset_fix_story_ids,
    set_fix_story_ids,
)

START = datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC)
GATE_REASONS = ["f1 above the champion's", "calibration within tolerance"]
STORY_MODELS = {
    "alert-prioritization": "fake-alert-prioritization",
    "model-performance": "fake-model-performance",
    "retraining": "fake-retraining",
    "deployment": "fake-deployment-promotion",
}


async def next_approval(
    services: Services,
    project_id: str,
    *,
    tool: str | None = None,
    proposal_kind: str | None = None,
) -> ApprovalView:
    """The pending approval of a call of `tool` (or of a proposal of `proposal_kind`), waiting
    for it to be asked."""
    async with asyncio.timeout(30):
        while True:
            for view in await services.approvals.pending(project_id):
                if (tool is None or view.tool == tool) and (
                    proposal_kind is None or view.proposal_kind == proposal_kind
                ):
                    return view
            await asyncio.sleep(0.05)


async def approve_all_until_idle(
    services: Services,
    http: httpx2.AsyncClient,
    project_id: str,
    agent: str,
) -> list[str]:
    """Approves what the agent asks until its turn ends. Returns the tools approved."""
    approved: list[str] = []
    async with asyncio.timeout(60):
        while True:
            pending = [v for v in await services.approvals.pending(project_id) if v.agent == agent]
            for view in pending:
                await services.approvals.decide(project_id, view.id, approve=True, note=None)
                approved.append(str(view.tool))
            session = (await http.get(f"/api/projects/{project_id}/agents/{agent}/session")).json()
            if not session.get("busy", False) and not pending:
                return approved
            await asyncio.sleep(0.05)


async def event_types(services: Services, project_id: str, subject_id: str | None) -> list[str]:
    records = await services.events.page(project_id, subject_id=subject_id, limit=200)
    return [r.type for r in records]


async def all_event_types(services: Services, project_id: str) -> list[str]:
    records = await services.events.page(project_id, limit=500)
    return [r.type for r in records]


async def wake_rows(services: Services, project_id: str, label: str) -> list[dict[str, Any]]:
    engine = await services.databases.project(project_id)
    async with engine.connect() as conn:
        rows = (
            (await conn.execute(select(agent_wakes).where(agent_wakes.c.label == label)))
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def git_show(git_dir: Path, spec: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(git_dir), "show", spec],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@dataclass
class Story:
    """The project of one story run and what its scenes share."""

    harness: E2EHarness
    services: Services
    http: httpx2.AsyncClient
    project_id: str
    promotion: PromotionMode
    mlflow_state: FakeMlflowState
    experiment: str
    data_path: Path
    bare: Path
    train_hook: HookView
    deploy_hook: HookView | None
    window_end: datetime

    @property
    def clock(self) -> SettableClock:
        return self.harness.clock


@dataclass
class Dispatched:
    """What the first scene leaves: the incident, its fix and the training request."""

    incident: IncidentView
    fix: FixRequestView
    request_id: str


@dataclass
class Linked:
    """What the second scene leaves: the saved report and, in person mode, the proposal's
    approval, which waits for the person."""

    report: ReportView
    promotion_approval: ApprovalView | None


@asynccontextmanager
async def story_env(
    tmp_path: Path,
    git_env: dict[str, str],
    promotion: PromotionMode,
    *,
    serve_origin: bool = False,
) -> AsyncGenerator[Story]:
    """The project of the fix story: the team's repository with a GitHub origin, a fake MLflow
    holding the champion, the scripted agents, the approved hooks, one learning pass.
    `serve_origin` serves the GitHub origin from a bare repository, for a task's fetch and push."""
    data_path = tmp_path / "hotel-data"
    repo_path = create_fix_repo(tmp_path / "repo", git_env)
    bare = make_bare_origin(tmp_path / "origin.git", git_env, repo_path)
    write_fixture_data_folder(data_path)

    mlflow_state = FakeMlflowState()
    experiment = seed_champion(mlflow_state, now=START)
    running_mlflow = start_fake_mlflow(mlflow_state)
    try:
        with ExitStack() as stack:
            if serve_origin:
                stack.enter_context(git_instead_of(tmp_path / "git.config", bare))
            async with (
                create_e2e_harness(tmp_path / "app-home", start_time=START) as harness,
                harness.started_app() as app,
            ):
                yield await setup_story(
                    harness,
                    app,
                    promotion=promotion,
                    repo_path=repo_path,
                    bare=bare,
                    data_path=data_path,
                    mlflow_url=running_mlflow.base_url,
                    mlflow_state=mlflow_state,
                    experiment=experiment,
                )
    finally:
        running_mlflow.stop()
        reset_fix_story_ids()


async def setup_story(
    harness: E2EHarness,
    app: FastAPI,
    *,
    promotion: PromotionMode,
    repo_path: Path,
    bare: Path,
    data_path: Path,
    mlflow_url: str,
    mlflow_state: FakeMlflowState,
    experiment: str,
) -> Story:
    http = harness.client(app)
    services: Services = app.state.services
    clock = harness.clock

    project = await services.workspace.add_local(str(repo_path))
    project_id = project.id
    await harness.setup_models(services, STORY_MODELS)
    discord_url = f"{harness.fake_discord.base_url}/api/webhooks/123/token_secret"
    await services.integration_settings.set_discord(enabled=True, webhook_url=discord_url)
    await services.github.set_token("gh_fake_fix_token")
    await services.connections.create(
        project_id,
        kind=LOCAL_FOLDER_KIND,
        name=CONNECTION_NAME,
        settings={"path": str(data_path)},
    )

    handoff = HandoffPayload.model_validate(fix_story_handoff_payload(mlflow_url, promotion))
    await services.handoffs.store(project_id, "prp_handoff_fix", handoff)
    for source in handoff.tracking:
        await services.tracking.create_source(
            project_id, source.type, source.settings.model_dump(), source.auth
        )

    hooks = await services.hooks.list(project_id)
    train_hook = next(h for h in hooks if h.spec.kind == "train")
    deploy_hook = next((h for h in hooks if h.spec.kind == "deploy"), None)
    assert (deploy_hook is None) == (promotion == "team_gate")
    assert train_hook.spec.deploys is (promotion == "team_gate")
    for hook in hooks:
        await services.hooks.approve(project_id, hook.id, by="user")

    window_end = clock.now() - timedelta(days=LABEL_MATURITY_DAYS + 1)
    window_start = window_end - timedelta(days=90)
    set_fix_story_ids(
        FixStoryIds(
            model=MODEL_NAME,
            train_hook_id=train_hook.id,
            deploy_hook_id=deploy_hook.id if deploy_hook is not None else None,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            candidate_version=CHALLENGER_VERSION,
            champion_version=CHAMPION_VERSION,
        )
    )

    await set_monitoring_settings(
        services.databases, project_id, monitoring_settings_payload(collection_interval_s=60)
    )
    harness.fake_discord.state.reset()
    # The test drives the jobs with run_due at each instant; the background loop stays off.
    await services.scheduler.stop()

    # The first pass learns the champion: the alias baseline the later moves are told from.
    await run_due(services, project_id, clock.now())
    return Story(
        harness=harness,
        services=services,
        http=http,
        project_id=project_id,
        promotion=promotion,
        mlflow_state=mlflow_state,
        experiment=experiment,
        data_path=data_path,
        bare=bare,
        train_hook=train_hook,
        deploy_hook=deploy_hook,
        window_end=window_end,
    )


async def decay_to_dispatch(story: Story) -> Dispatched:
    """Scene 1: a confirmed decay -> RequestFix -> the fix -> the train hook asks -> dispatch."""
    services, http, project_id = story.services, story.http, story.project_id
    harness = story.harness

    story.clock.advance(timedelta(minutes=30))
    write_results_record(
        story.data_path / "results",
        label_check_record(ran_at=story.clock.now()).model_dump(mode="json"),
        "results.jsonl",
    )
    await run_due(services, project_id, story.clock.now())
    await wait_for_agent_idle(http, project_id, "model-performance")

    incident_page = await services.incidents.list(project_id)
    assert incident_page.total == 1
    incident = incident_page.rows[0]
    assert incident.category == "model_performance"
    assert incident.model == MODEL_NAME

    # Model Performance called RequestFix: one fix, requested by it, on the incident.
    fixes = await services.fixes.list_fixes(project_id, incident_id=incident.id)
    assert len(fixes) == 1
    assert fixes[0].requested_by == "model-performance"

    # Retraining, woken with fix_request, chose retrain and asks before the train hook runs.
    approval = await next_approval(services, project_id, tool=RUN_HOOK)
    assert approval.agent == "retraining"
    fix = await services.fixes.get(project_id, fixes[0].id)
    assert fix is not None
    assert fix.choice == "retrain"
    assert set(fix.why_not) == {"re_threshold", "recalibrate", "decline"}
    assert harness.fake_github.state.dispatches == []

    # After the person's approval the guards pass and the fake GitHub gets one dispatch on the
    # default branch with the request ID filled in.
    await services.approvals.decide(project_id, approval.id, approve=True, note=None)
    await wait_for_agent_idle(http, project_id, "retraining")
    dispatches = harness.fake_github.state.dispatches
    assert len(dispatches) == 1
    dispatch = dispatches[0]
    assert dispatch["repo"] == GITHUB_REPOSITORY
    assert dispatch["workflow"] == TRAIN_WORKFLOW
    assert dispatch["body"]["ref"] == "main"
    request_id = dispatch["body"]["inputs"]["request_id"]
    assert request_id.startswith("trq_")
    assert dispatch["body"]["inputs"]["window_end"] == story.window_end.isoformat()
    request = await services.training.request(project_id, request_id)
    assert request is not None
    assert request.status == "started"
    assert request.fix_id == fix.id
    return Dispatched(incident=incident, fix=fix, request_id=request_id)


async def pipeline_links(story: Story, dispatched: Dispatched) -> Linked:
    """Scene 2: the team's pipeline runs; the tagged run links the request; Retraining, woken
    again, reads the evaluation and saves the report."""
    services, http, project_id = story.services, story.http, story.project_id
    team_gate = story.promotion == "team_gate"

    story.clock.advance(timedelta(minutes=45))
    play_pipeline(
        story.mlflow_state,
        request_id=dispatched.request_id,
        now=story.clock.now(),
        experiment=story.experiment,
        decision="promote" if team_gate else None,
        reasons=GATE_REASONS if team_gate else None,
        # In team_gate the pipeline deploys as the run's end; in person mode it never moves
        # the alias before the deploy hook.
        move_alias=team_gate,
    )
    await run_due(services, project_id, story.clock.now())

    linked = await services.training.request(project_id, dispatched.request_id)
    assert linked is not None
    assert linked.status == "linked"
    assert linked.run_key == CHALLENGER_RUN
    assert "training.linked" in await event_types(services, project_id, dispatched.incident.id)
    # The train hook's alias check: deploys false and the alias did not move, so it is verified.
    train_hook = await services.hooks.get(project_id, story.train_hook.id)
    assert train_hook is not None
    assert train_hook.status == "active"
    # A hook declaring deploys: false is verified once its linked run left the alias alone;
    # one declaring deploys: true is never checked.
    assert train_hook.deploys_verified is (True if story.promotion == "person" else None)

    promotion_approval: ApprovalView | None = None
    if story.promotion == "person":
        # Retraining then proposes the promotion, which waits for the person.
        promotion_approval = await next_approval(services, project_id, proposal_kind=PROMOTION_KIND)
    else:
        await wait_for_agent_idle(http, project_id, "retraining")

    report = await services.reports.for_candidate(project_id, MODEL_NAME, CHALLENGER_VERSION)
    assert report is not None
    assert set(report.draft.assessments) == set(REQUIRED_REPORT_FIELDS)
    assert all(a.value is not None for a in report.draft.assessments.values())
    assert report.draft.fix_id == dispatched.fix.id
    assert report.evaluation is not None
    assert report.evaluation.payload.champion_version == CHAMPION_VERSION
    fix = await services.fixes.get(project_id, dispatched.fix.id)
    assert fix is not None
    assert fix.status == "done"
    return Linked(report=report, promotion_approval=promotion_approval)


async def promotion_applied(story: Story, linked: Linked) -> None:
    """Scene 3: the promotion of the mode."""
    services, http, project_id = story.services, story.http, story.project_id
    harness = story.harness

    if story.promotion == "person":
        assert linked.promotion_approval is not None
        assert story.deploy_hook is not None
        # The proposal waits for the person; nothing was deployed or promoted meanwhile.
        assert await services.promotions.history(project_id, model=MODEL_NAME) == []
        assert await services.hooks.runs(project_id, kinds=["deploy"]) == []

        await services.approvals.decide(
            project_id, linked.promotion_approval.id, approve=True, note=None
        )
        await wait_for_agent_idle(http, project_id, "retraining")

        history = await services.promotions.history(project_id, model=MODEL_NAME)
        assert len(history) == 1
        assert history[0].mode == "person"
        assert history[0].decided_by == "person"
        assert history[0].status == "applied"
        assert history[0].candidate_version == CHALLENGER_VERSION
        assert history[0].champion_version == CHAMPION_VERSION
        assert history[0].report_id == linked.report.id

        # Re-validated, then the deploy hook ran on the approval's basis.
        deploy_runs = await services.hooks.runs(project_id, kinds=["deploy"])
        assert len(deploy_runs) == 1
        assert deploy_runs[0].hook_id == story.deploy_hook.id
        assert deploy_runs[0].basis == "approval"
        assert deploy_runs[0].ok is True
        assert any(
            f"deploy.py {CHALLENGER_VERSION}" in " ".join(call)
            for call in harness.docker_runner.calls
        )
        assert "promotion.applied" in await all_event_types(services, project_id)
        return

    # team_gate: the team's own gate decided. The app read the decision from the evaluation
    # record, deployed nothing and told the first on-call person.
    history = await services.promotions.history(project_id, model=MODEL_NAME)
    assert len(history) == 1
    assert history[0].mode == "team_gate"
    assert history[0].decided_by == "team_gate"
    assert history[0].status == "applied"
    assert history[0].candidate_version == CHALLENGER_VERSION
    assert await services.hooks.runs(project_id, kinds=["deploy"]) == []
    assert not any("deploy" in " ".join(call) for call in harness.docker_runner.calls)
    assert "promotion.applied" in await all_event_types(services, project_id)

    # The incident's page went out before; the gate's decision is one more message.
    gate_posts = [p for p in harness.fake_discord.state.posts if TEAM_GATE_TITLE in json.dumps(p)]
    assert len(gate_posts) == 1
    text = json.dumps(gate_posts[0])
    assert f"{TEAM_GATE_TITLE} promote" in text
    assert GATE_REASONS[0] in text
    engine = await services.databases.project(project_id)
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(notification_log).where(notification_log.c.kind == "promotion")
                )
            )
            .mappings()
            .all()
        )
    assert [r["person"] for r in rows] == [FIRST_ON_CALL]


async def alias_moves(story: Story) -> None:
    """Scene 4: the production alias moves to the new version. In person mode the team's deploy
    step moves it after the deploy hook; in team_gate the pipeline moved it as the run's end."""
    if story.promotion == "person":
        story.clock.advance(timedelta(minutes=5))
        story.mlflow_state.set_alias(MODEL_NAME, PRODUCTION_ALIAS, CHALLENGER_VERSION)
        await run_due(story.services, story.project_id, story.clock.now())
    await wait_for_agent_idle(story.http, story.project_id, "deployment")


def moved_to_challenger(wake: dict[str, Any]) -> bool:
    return f"{MODEL_NAME}:{PRODUCTION_ALIAS}:{CHAMPION_VERSION}:{CHALLENGER_VERSION}:" in str(
        wake["key"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("promotion", ["person", "team_gate"])
async def test_incident_to_fix_to_promotion(
    tmp_path: Path, git_env: dict[str, str], promotion: PromotionMode
) -> None:
    """Scenes 1 to 4 once per promotion mode."""
    async with story_env(tmp_path, git_env, promotion) as story:
        dispatched = await decay_to_dispatch(story)
        linked = await pipeline_links(story, dispatched)
        await promotion_applied(story, linked)
        await alias_moves(story)

        # The move of the alias to the new version woke Deployment & Rollback once with
        # post_deploy, and its scripted turn registered the follow-up.
        wakes = await wake_rows(story.services, story.project_id, "post_deploy")
        # Only the wake of the real move: the baseline row's wake is the separate #649 failure.
        moved = [w for w in wakes if moved_to_challenger(w)]
        assert len(moved) == 1
        assert moved[0]["agent"] == "deployment"
        assert NEW_VERSION_LINE in moved[0]["text"]
        pending = await story.services.wakeups.list_pending(story.project_id, agent="deployment")
        assert pending
        assert {w.note for w in pending} == {POST_DEPLOY_FOLLOW_UP}

        # Later passes do not wake it again for the same move.
        story.clock.advance(timedelta(minutes=10))
        await run_due(story.services, story.project_id, story.clock.now())
        wakes = await wake_rows(story.services, story.project_id, "post_deploy")
        # Filtered as above (#649).
        assert len([w for w in wakes if moved_to_challenger(w)]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("promotion", ["person", "team_gate"])
async def test_alias_move_wakes_deployment_once(
    tmp_path: Path, git_env: dict[str, str], promotion: PromotionMode
) -> None:
    """Both modes: the whole story wakes Deployment & Rollback exactly once with post_deploy,
    for the move of the alias and for nothing else.

    Fails today because of bug #649: the champion's first deployment row (none to the champion)
    is recorded by the second tracking sync, after the post-deploy cursor was seeded on the first
    pass, so it counts as a move and wakes Deployment & Rollback a second time."""
    async with story_env(tmp_path, git_env, promotion) as story:
        dispatched = await decay_to_dispatch(story)
        linked = await pipeline_links(story, dispatched)
        await promotion_applied(story, linked)
        await alias_moves(story)

        wakes = await wake_rows(story.services, story.project_id, "post_deploy")
        assert len(wakes) == 1
        assert moved_to_challenger(wakes[0])


@pytest.mark.asyncio
async def test_deployment_opens_a_pull_request(tmp_path: Path, git_env: dict[str, str]) -> None:
    """Scene 5: Deployment & Rollback, messaged by the test, improves the evaluation step through
    StartTask, Write and OpenPullRequest against the fake GitHub (a local project with a GitHub
    origin: the fake has no route to read a repository)."""
    async with story_env(tmp_path, git_env, "person", serve_origin=True) as story:
        services, http, project_id = story.services, story.http, story.project_id
        sent = await http.post(
            f"/api/projects/{project_id}/agents/deployment/session/messages",
            json={"text": EVALUATION_PR_MESSAGE},
        )
        assert sent.status_code == 201, sent.text
        approved = await approve_all_until_idle(services, http, project_id, "deployment")
        assert approved == [START_TASK, WRITE, OPEN_PULL_REQUEST]

        pulls = [
            r
            for r in story.harness.fake_github.state.requests
            if r["method"] == "POST" and r["path"] == f"/repos/{GITHUB_REPOSITORY}/pulls"
        ]
        assert len(pulls) == 1
        assert pulls[0]["body"]["title"] == EVALUATION_PR_TITLE
        assert pulls[0]["body"]["base"] == "main"
        # The pushed branch holds the written file.
        pushed = await asyncio.to_thread(
            git_show, story.bare, f"{pulls[0]['body']['head']}:{EVALUATION_FILE}"
        )
        assert EVALUATION_FIELD in pushed
