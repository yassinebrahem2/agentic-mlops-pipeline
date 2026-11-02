"""Tests for the deployments view (I40) and the reports and promotions query providers (#500)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from pydantic import BaseModel

from mlops_agents.agents.runtime.contract import TaskView
from mlops_agents.agents.tasks.contract import Tasks
from mlops_agents.agents.tools.base import ToolContext
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.integrations.github import api
from mlops_agents.integrations.github.auth import GitHubAuth
from mlops_agents.monitoring.hooks.contract import HookRunView, Hooks
from mlops_agents.monitoring.incidents.query import MonitoringQueryTool
from mlops_agents.monitoring.query import MonitoringQuery, QueryProviders
from mlops_agents.monitoring.untrusted import untrusted
from mlops_agents.promotion.constants import REQUIRED_REPORT_FIELDS
from mlops_agents.promotion.contract import (
    EvaluationReader,
    EvaluationView,
    Promotions,
    PromotionView,
    ReportDraft,
    ReportField,
    ReportView,
)
from mlops_agents.promotion.deployments import (
    DeploymentsService,
    PromotionsQueryProvider,
    ReportsQueryProvider,
)
from mlops_agents.promotion.reports import SqliteReports
from mlops_agents.proposals.contract import Handoffs, HandoffView
from mlops_agents.proposals.payloads import HandoffPayload
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import reports as reports_table
from mlops_agents.tracking.contract import (
    DeploymentRow,
    ModelVersionRow,
    Page,
    Tracking,
    TrackingQuery,
)
from mlops_agents.workspace.refs import RepoRef
from tests.agents.gate_helpers import FakeFileAccess
from tests.api.conftest import add_local_project, signed_in, started_app
from tests.fakes.fake_github import FakeGitHubState, RunningFakeGitHub, start_fake_github
from tests.fakes.fake_tasks import FakeTasks
from tests.proposals.samples import handoff as sample_handoff
from tests.proposals.samples import model as sample_model

PROJECT_ID = "prj_deploymentstest"
MODEL = "fraud_detection"
REPO = RepoRef(owner="testorg", repo="testrepo")
TOKEN = "ghp_deployments_secret_token"
T0 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)


class FakeHandoffs:
    def __init__(self, handoff: HandoffView | None = None) -> None:
        self.handoff = handoff

    async def current(self, project_id: str) -> HandoffView | None:
        return self.handoff


class FakeTracking:
    def __init__(self) -> None:
        self.versions: list[ModelVersionRow] = []
        self.deployments: list[DeploymentRow] = []

    async def query(self, project_id: str, q: TrackingQuery) -> Page[Any]:
        rows: list[Any] = []
        if q.what == "versions":
            rows = [v for v in self.versions if q.model is None or v.model == q.model]
        elif q.what == "deployments":
            # As the tracker answers: newest first, filtered by model and since, one page.
            rows = sorted(
                (
                    d
                    for d in self.deployments
                    if (q.model is None or d.model == q.model)
                    and (q.since is None or d.seen_at >= q.since)
                ),
                key=lambda d: d.seen_at,
                reverse=True,
            )
        page = rows[q.offset : q.offset + q.limit]
        return Page(rows=page, total=len(rows), truncated=q.offset + len(page) < len(rows))


class FakePromotions:
    def __init__(self, items: list[PromotionView] | None = None) -> None:
        self.items = list(items or [])

    async def history(self, project_id: str, model: str | None = None) -> list[PromotionView]:
        return [p for p in self.items if model is None or p.model == model]


class FakeHooks:
    def __init__(self, runs: list[HookRunView] | None = None) -> None:
        self.items = list(runs or [])

    async def runs(
        self,
        project_id: str,
        *,
        kinds: Sequence[str] | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[HookRunView]:
        return [r for r in self.items if kinds is None or r.kind in kinds]


class RepoTasks(FakeTasks):
    def __init__(self, tasks: list[TaskView] | None, repo: RepoRef | None) -> None:
        super().__init__(tasks)
        self.repo = repo

    async def repo_ref(self, project_id: str) -> RepoRef | None:
        return self.repo


class FakeGitHubAuth:
    def __init__(self, token: str | None) -> None:
        self.value = token

    async def token(self) -> str | None:
        return self.value


class NoEvaluations:
    async def for_candidate(
        self, project_id: str, model: str, version: str
    ) -> EvaluationView | None:
        return None

    async def latest(self, project_id: str, model: str) -> EvaluationView | None:
        return None


def _service(
    http: httpx2.AsyncClient,
    *,
    handoff: HandoffView | None = None,
    tracking: FakeTracking | None = None,
    promotions: list[PromotionView] | None = None,
    runs: list[HookRunView] | None = None,
    tasks: list[TaskView] | None = None,
    repo: RepoRef | None = REPO,
    token: str | None = TOKEN,
) -> DeploymentsService:
    return DeploymentsService(
        handoffs=cast(Handoffs, FakeHandoffs(handoff)),
        tracking=cast(Tracking, tracking or FakeTracking()),
        promotions=cast(Promotions, FakePromotions(promotions)),
        hooks=cast(Hooks, FakeHooks(runs)),
        tasks=cast(Tasks, RepoTasks(tasks, repo)),
        github=cast(GitHubAuth, FakeGitHubAuth(token)),
        http=http,
    )


def _handoff(environments: dict[str, str]) -> HandoffView:
    production = next(alias for alias, env in environments.items() if env == "production")
    signal = {"kind": "tracker_alias", "ref": production}
    raw = sample_handoff(
        models=[sample_model(MODEL, environments=environments, production_signal=signal)]
    )
    return HandoffView(
        proposal_id="prp_1",
        version=1,
        approved=T0,
        payload=HandoffPayload.model_validate(raw),
    )


def _promotion(
    id: str, at: datetime, decided_by: str, candidate: str, champion: str | None
) -> PromotionView:
    return PromotionView.model_validate(
        {
            "id": id,
            "model": MODEL,
            "candidate_version": candidate,
            "champion_version": champion,
            "mode": "team_gate" if decided_by == "team_gate" else "person",
            "decided_by": decided_by,
            "status": "applied",
            "report_id": f"rep_{id}",
            "created": at - timedelta(minutes=1),
            "applied_at": at,
        }
    )


def _run(
    id: str,
    kind: str,
    started: datetime,
    *,
    basis: str = "rule",
    rule_id: str | None = None,
    basis_ref: str | None = None,
    ok: bool | None = True,
    model: str | None = MODEL,
) -> HookRunView:
    return HookRunView.model_validate(
        {
            "id": id,
            "hook_id": f"hk_{kind}",
            "kind": kind,
            "model": model,
            "reason": "the error rate rose",
            "basis": basis,
            "rule_id": rule_id,
            "basis_ref": basis_ref,
            "ok": ok,
            "started": started,
            "ended": started + timedelta(seconds=5),
        }
    )


def _move(
    alias: str,
    from_version: str | None,
    to_version: str | None,
    at: datetime,
    *,
    model: str = MODEL,
    baseline: bool = False,
) -> DeploymentRow:
    return DeploymentRow(
        source_id="src_1",
        model=model,
        alias=alias,
        from_version=from_version,
        to_version=to_version,
        seen_at=at,
        baseline=baseline,
    )


def _task(id: str, pr_number: int | None, review_kind: str | None) -> TaskView:
    return TaskView.model_validate(
        {
            "id": id,
            "agent": "retraining",
            "title": f"Task {id}",
            "branch": f"agent/{id}",
            "status": "active",
            "review_kind": review_kind,
            "pr_number": pr_number,
            "pr_url": None if pr_number is None else f"https://github.com/o/r/pull/{pr_number}",
        }
    )


@pytest.fixture
def fake_github() -> Iterator[RunningFakeGitHub]:
    running = start_fake_github(FakeGitHubState())
    original_api = api.GITHUB_API
    api.GITHUB_API = running.base_url
    try:
        yield running
    finally:
        api.GITHUB_API = original_api
        running.stop()


@pytest.mark.anyio
async def test_environments_from_a_two_alias_handoff() -> None:
    tracking = FakeTracking()
    tracking.versions.append(
        ModelVersionRow(
            source_id="src_1",
            model=MODEL,
            version="v2",
            run_key="run_1",
            aliases=["champion"],
            created=T0,
            evaluation="present",
        )
    )
    handoff = _handoff({"champion": "production", "challenger": "staging"})

    async with httpx2.AsyncClient() as http:
        view = await _service(http, handoff=handoff, tracking=tracking).get_deployments(PROJECT_ID)

    assert sorted((e.model, e.alias, e.environment, e.version) for e in view.environments) == [
        (MODEL, "challenger", "staging", None),
        (MODEL, "champion", "production", "v2"),
    ]


@pytest.mark.anyio
async def test_decisions_newest_first_with_who_decided() -> None:
    tracking = FakeTracking()
    tracking.deployments += [
        _move("production", "v2", "v1", T0 + timedelta(minutes=20, seconds=30)),
        _move("production", "v1", "v0", T0 + timedelta(minutes=30, seconds=30)),
    ]
    promotions = [
        _promotion("prm_1", T0 + timedelta(minutes=10), "person", "v2", "v1"),
        _promotion("prm_4", T0 + timedelta(minutes=40), "team_gate", "v3", "v0"),
    ]
    runs = [
        _run("hrun_rb", "rollback", T0 + timedelta(minutes=20), rule_id="rul_1"),
        _run(
            "hrun_fb",
            "fall_back",
            T0 + timedelta(minutes=30),
            basis="approval",
            basis_ref="apr_fb",
        ),
    ]

    async with httpx2.AsyncClient() as http:
        view = await _service(
            http, tracking=tracking, promotions=promotions, runs=runs
        ).get_deployments(PROJECT_ID)

    rows = [
        (d.at, d.kind, d.from_version, d.to_version, d.decided_by, d.decided_by_ref, d.report_id)
        for d in view.decisions
    ]
    assert rows == [
        (T0 + timedelta(minutes=40), "promotion", "v0", "v3", "team_gate", None, "rep_prm_4"),
        (T0 + timedelta(minutes=30, seconds=5), "fall_back", "v1", "v0", "person", "apr_fb", None),
        (T0 + timedelta(minutes=20, seconds=5), "rollback", "v2", "v1", "rule", "rul_1", None),
        (T0 + timedelta(minutes=10), "promotion", "v1", "v2", "person", None, "rep_prm_1"),
    ]


@pytest.mark.anyio
async def test_hook_run_versions_come_from_the_next_unclaimed_production_move() -> None:
    # The handoff maps the tracker alias "live" to production; "canary" is not production.
    handoff = _handoff({"live": "production", "canary": "staging"})
    first, later = T0 + timedelta(minutes=10), T0 + timedelta(minutes=30)
    tracking = FakeTracking()
    tracking.deployments += [
        _move("live", "v3", "v4", first - timedelta(minutes=1)),  # before every run
        _move("live", "v4", None, first + timedelta(seconds=5)),  # an alias removal
        _move("canary", "v9", "v8", first + timedelta(seconds=10)),  # not a production alias
        _move("live", "v4", "v3", first + timedelta(seconds=20)),
        _move("live", "v3", "v2", first + timedelta(seconds=40)),
    ]
    runs = [
        _run("hrun_3", "rollback", later, rule_id="rul_3"),
        _run("hrun_2", "rollback", first + timedelta(seconds=1), rule_id="rul_2"),
        _run("hrun_1", "rollback", first, rule_id="rul_1"),
    ]

    async with httpx2.AsyncClient() as http:
        view = await _service(http, handoff=handoff, tracking=tracking, runs=runs).get_deployments(
            PROJECT_ID
        )

    versions = {d.decided_by_ref: (d.from_version, d.to_version) for d in view.decisions}
    assert versions == {
        "rul_1": ("v4", "v3"),
        "rul_2": ("v3", "v2"),
        "rul_3": (None, None),
    }
    assert sorted((e.alias, e.environment, e.version) for e in view.environments) == [
        ("canary", "staging", None),
        ("live", "production", None),
    ]


@pytest.mark.anyio
async def test_hook_runs_get_no_versions_when_the_moves_since_them_overflow_a_page(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tracking = FakeTracking()
    tracking.deployments.append(_move("production", "v1", "v0", T0 + timedelta(seconds=1)))
    # More moves after the run than one tracker page holds: the run's own move is cut off.
    tracking.deployments += [
        _move("production", f"v{i}", f"v{i + 1}", T0 + timedelta(minutes=1, seconds=i))
        for i in range(500)
    ]
    runs = [
        _run("hrun_old", "rollback", T0, rule_id="rul_old"),
        _run("hrun_new", "rollback", T0 + timedelta(minutes=1), rule_id="rul_new"),
    ]

    with caplog.at_level(logging.WARNING, logger="mlops_agents.promotion.deployments"):
        async with httpx2.AsyncClient() as http:
            view = await _service(http, tracking=tracking, runs=runs).get_deployments(PROJECT_ID)

    versions = {d.decided_by_ref: (d.from_version, d.to_version) for d in view.decisions}
    assert versions == {"rul_old": (None, None), "rul_new": (None, None)}
    assert any(MODEL in r.getMessage() for r in caplog.records)


@pytest.mark.anyio
async def test_production_moves_skips_baseline_rows() -> None:
    first = T0 + timedelta(minutes=1)
    tracking = FakeTracking()
    # Baseline row (e.g. initial champion) followed by an actual move
    tracking.deployments = [
        _move("production", None, "v1", first, baseline=True),
        _move("production", "v1", "v2", first + timedelta(seconds=20), baseline=False),
    ]
    runs = [
        _run("hrun_1", "rollback", first, rule_id="rul_1"),
    ]

    async with httpx2.AsyncClient() as http:
        view = await _service(http, tracking=tracking, runs=runs).get_deployments(PROJECT_ID)

    versions = {d.decided_by_ref: (d.from_version, d.to_version) for d in view.decisions}
    # The rollback claims the actual move v1 -> v2, NOT the baseline None -> v1!
    assert versions == {
        "rul_1": ("v1", "v2"),
    }


@pytest.mark.anyio
async def test_hook_runs_that_failed_name_no_model_or_cite_a_policy_are_left_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runs = [
        _run("hrun_ok", "fall_back", T0, basis="none"),
        _run("hrun_failed", "rollback", T0, rule_id="rul_1", ok=False),
        _run("hrun_running", "rollback", T0, rule_id="rul_1", ok=None),
        _run("hrun_nomodel", "rollback", T0, rule_id="rul_1", model=None),
        _run("hrun_policy", "rollback", T0, basis="policy", basis_ref="pol_1"),
    ]

    with caplog.at_level(logging.WARNING, logger="mlops_agents.promotion.deployments"):
        async with httpx2.AsyncClient() as http:
            view = await _service(http, runs=runs).get_deployments(PROJECT_ID)

    assert [(d.kind, d.decided_by, d.decided_by_ref) for d in view.decisions] == [
        ("fall_back", "agent_bypass", None)
    ]
    assert any("hrun_policy" in r.getMessage() for r in caplog.records)


@pytest.mark.anyio
async def test_each_run_takes_its_own_models_move() -> None:
    other = "churn_model"
    tracking = FakeTracking()
    tracking.deployments += [
        # The other model's move falls between this model's runs, before this model's moves.
        _move("production", "b1", "b0", T0 + timedelta(seconds=5), model=other),
        _move("production", "a2", "a1", T0 + timedelta(seconds=20)),
        _move("production", "a1", "a0", T0 + timedelta(seconds=30)),
    ]
    runs = [
        _run("hrun_a1", "rollback", T0, rule_id="rul_a1"),
        _run("hrun_b", "rollback", T0 + timedelta(seconds=3), rule_id="rul_b", model=other),
        _run("hrun_a2", "rollback", T0 + timedelta(seconds=10), rule_id="rul_a2"),
    ]

    async with httpx2.AsyncClient() as http:
        view = await _service(http, tracking=tracking, runs=runs).get_deployments(PROJECT_ID)

    versions = {d.decided_by_ref: (d.model, d.from_version, d.to_version) for d in view.decisions}
    assert versions == {
        "rul_a1": (MODEL, "a2", "a1"),
        "rul_a2": (MODEL, "a1", "a0"),
        "rul_b": (other, "b1", "b0"),
    }


@pytest.mark.anyio
async def test_moves_before_the_oldest_run_do_not_fill_the_page() -> None:
    tracking = FakeTracking()
    # A page's worth of moves before the run: read with no lower bound they would overflow it.
    tracking.deployments += [
        _move("production", f"old{i}", f"old{i + 1}", T0 - timedelta(hours=1, seconds=-i))
        for i in range(500)
    ]
    tracking.deployments.append(_move("production", "v1", "v0", T0 + timedelta(seconds=1)))
    runs = [_run("hrun_rb", "rollback", T0, rule_id="rul_1")]

    async with httpx2.AsyncClient() as http:
        view = await _service(http, tracking=tracking, runs=runs).get_deployments(PROJECT_ID)

    assert [(d.from_version, d.to_version) for d in view.decisions] == [("v1", "v0")]


@pytest.mark.anyio
async def test_other_models_moves_do_not_fill_the_page() -> None:
    tracking = FakeTracking()
    tracking.deployments.append(_move("production", "v1", "v0", T0 + timedelta(seconds=1)))
    # A page's worth of another model's moves after the run: read with no model filter they
    # would overflow the page and leave the run with no versions.
    tracking.deployments += [
        _move("production", f"c{i}", f"c{i + 1}", T0 + timedelta(seconds=2 + i), model="other")
        for i in range(500)
    ]
    runs = [_run("hrun_rb", "rollback", T0, rule_id="rul_1")]

    async with httpx2.AsyncClient() as http:
        view = await _service(http, tracking=tracking, runs=runs).get_deployments(PROJECT_ID)

    assert [(d.from_version, d.to_version) for d in view.decisions] == [("v1", "v0")]


@pytest.mark.anyio
async def test_open_prs_carry_their_labels_from_github(fake_github: RunningFakeGitHub) -> None:
    repo = f"{REPO.owner}/{REPO.repo}"
    fake_github.state.prs[(repo, 42)] = {
        "number": 42,
        "state": "open",
        "labels": [{"name": "pipeline"}, {"name": "needs-review"}],
    }
    fake_github.state.prs[(repo, 43)] = {"number": 43, "state": "open"}
    tasks = [
        _task("tsk_1", 42, "github"),
        _task("tsk_2", 43, "github"),
        _task("tsk_3", None, "local"),
        _task("tsk_4", None, None),
    ]

    async with httpx2.AsyncClient() as http:
        view = await _service(http, tasks=tasks).get_deployments(PROJECT_ID)

    assert [(p.number, p.title, p.agent, p.labels) for p in view.open_prs] == [
        (42, "Task tsk_1", "retraining", ["pipeline", "needs-review"]),
        (43, "Task tsk_2", "retraining", []),
    ]
    reads = [r for r in fake_github.state.requests if r["path"] == f"/repos/{repo}/pulls"]
    assert len(reads) == 1
    assert reads[0]["params"]["state"] == "open"


@pytest.mark.anyio
async def test_open_prs_answer_without_labels_when_github_fails(
    fake_github: RunningFakeGitHub, caplog: pytest.LogCaptureFixture
) -> None:
    fake_github.state.prs[(f"{REPO.owner}/{REPO.repo}", 42)] = {
        "number": 42,
        "state": "open",
        "labels": [{"name": "pipeline"}],
    }
    fake_github.state.simulate_permission_error["pull requests: write"] = 500

    with caplog.at_level(logging.WARNING, logger="mlops_agents.promotion.deployments"):
        async with httpx2.AsyncClient() as http:
            view = await _service(http, tasks=[_task("tsk_1", 42, "github")]).get_deployments(
                PROJECT_ID
            )

    assert [(p.number, p.labels) for p in view.open_prs] == [(42, [])]
    assert caplog.records
    assert all(TOKEN not in r.getMessage() for r in caplog.records)


@pytest.mark.anyio
@pytest.mark.parametrize(("token", "repo"), [(None, REPO), (TOKEN, None)])
async def test_open_prs_answer_without_labels_with_no_token_or_no_repository(
    fake_github: RunningFakeGitHub, token: str | None, repo: RepoRef | None
) -> None:
    tracking = FakeTracking()
    tracking.deployments.append(_move("production", "v2", "v1", T0 + timedelta(seconds=1)))
    runs = [_run("hrun_rb", "rollback", T0, rule_id="rul_1")]

    async with httpx2.AsyncClient() as http:
        view = await _service(
            http,
            tracking=tracking,
            runs=runs,
            tasks=[_task("tsk_1", 42, "github")],
            repo=repo,
            token=token,
        ).get_deployments(PROJECT_ID)

    assert [(p.number, p.labels) for p in view.open_prs] == [(42, [])]
    assert [(d.from_version, d.to_version) for d in view.decisions] == [("v2", "v1")]
    assert fake_github.state.requests == []


async def _save_reports(databases: Databases, count: int) -> tuple[SqliteReports, list[ReportView]]:
    store = SqliteReports(
        databases, cast(EvaluationReader, NoEvaluations()), SqliteEventLog(databases)
    )
    saved: list[ReportView] = []
    for i in range(count):
        assessments = {
            field: ReportField(value=f"<data>assessment {i} of {field}")
            for field in REQUIRED_REPORT_FIELDS
        }
        draft = ReportDraft(model=MODEL, candidate_version=f"v{i}", assessments=assessments)
        saved.append(await store.save(PROJECT_ID, draft, author="agent:retraining"))
    return store, saved


@pytest.mark.anyio
async def test_reports_provider_pages_with_limit_and_offset(databases: Databases) -> None:
    store, saved = await _save_reports(databases, 5)
    provider = ReportsQueryProvider(databases, store)

    first = await provider.query(
        PROJECT_ID, MonitoringQuery(what="reports", model=MODEL, limit=2, offset=0)
    )
    last = await provider.query(
        PROJECT_ID, MonitoringQuery(what="reports", model=MODEL, limit=2, offset=4)
    )
    middle = await provider.query(
        PROJECT_ID, MonitoringQuery(what="reports", model=MODEL, limit=2, offset=2)
    )
    other_model = await provider.query(PROJECT_ID, MonitoringQuery(what="reports", model="other"))

    assert (first.total, len(first.rows), first.truncated) == (5, 2, True)
    assert (last.total, len(last.rows), last.truncated) == (5, 1, False)
    paged_ids = [r.id for r in [*first.rows, *middle.rows, *last.rows]]
    assert sorted(paged_ids) == sorted(r.id for r in saved)
    assert other_model.total == 0
    assert other_model.rows == []
    report = first.rows[0]
    assert report == await store.get(PROJECT_ID, report.id)
    by_id = await provider.query(PROJECT_ID, MonitoringQuery(what="reports", id=report.id))
    assert [r.id for r in by_id.rows] == [report.id]


@pytest.mark.anyio
async def test_reports_provider_filters_by_since(databases: Databases) -> None:
    store, saved = await _save_reports(databases, 3)
    engine = await databases.project(PROJECT_ID)
    async with engine.begin() as conn:
        await conn.execute(
            reports_table.update()
            .where(reports_table.c.id == saved[0].id)
            .values(created="2026-01-01T00:00:00.000Z")
        )
    provider = ReportsQueryProvider(databases, store)

    page = await provider.query(
        PROJECT_ID, MonitoringQuery(what="reports", since=datetime(2026, 6, 1, tzinfo=UTC))
    )

    assert page.total == 2
    assert sorted(r.id for r in page.rows) == sorted(r.id for r in saved[1:])


def _refused(i: int) -> PromotionView:
    return PromotionView.model_validate(
        {
            "id": f"prm_{i}",
            "model": MODEL,
            "candidate_version": f"v{i}",
            "mode": "person",
            "decided_by": "person",
            "status": "refused",
            "refusals": [f"<data>refusal {i}"],
            "created": T0 + timedelta(minutes=i),
        }
    )


@pytest.mark.anyio
async def test_promotions_provider_pages_with_limit_and_offset() -> None:
    items = [_refused(i) for i in range(5)]
    provider = PromotionsQueryProvider(cast(Promotions, FakePromotions(items)))

    first = await provider.query(
        PROJECT_ID, MonitoringQuery(what="promotions", model=MODEL, limit=2, offset=0)
    )
    last = await provider.query(
        PROJECT_ID, MonitoringQuery(what="promotions", model=MODEL, limit=2, offset=4)
    )

    assert (first.total, first.truncated) == (5, True)
    assert first.rows == items[0:2]
    assert (last.total, last.truncated) == (5, False)
    assert last.rows == items[4:5]
    assert items[0].refusals == ["<data>refusal 0"]


@pytest.mark.anyio
async def test_promotions_provider_filters_by_id_status_and_since() -> None:
    applied = _promotion("prm_applied", T0 + timedelta(minutes=2), "person", "v9", "v1")
    items = [*(_refused(i) for i in range(5)), applied]
    provider = PromotionsQueryProvider(cast(Promotions, FakePromotions(items)))

    async def ids(**filters: Any) -> list[str]:
        page = await provider.query(
            PROJECT_ID, MonitoringQuery.model_validate({"what": "promotions", **filters})
        )
        assert page.total == len(page.rows)
        return [p.id for p in page.rows]

    assert await ids(id="prm_2") == ["prm_2"]
    assert await ids(status="applied") == ["prm_applied"]
    assert await ids(since=T0 + timedelta(minutes=3)) == ["prm_3", "prm_4"]
    assert await ids(status="refused", since=T0 + timedelta(minutes=4)) == ["prm_4"]


def _ctx() -> ToolContext:
    return ToolContext(
        project_id=PROJECT_ID,
        agent="diagnoser",
        session_id="ses_1",
        turn_id="trn_1",
        files=FakeFileAccess(),
        task=None,
        tool_call_id="call_1",
    )


def _wrapped_once(rows: Sequence[BaseModel]) -> str:
    rows_json = json.dumps([r.model_dump(mode="json") for r in rows], indent=2, default=str)
    return (
        f"total: {len(rows)}\ntruncated: False\n{untrusted(rows_json, source='monitoring_query')}"
    )


@pytest.mark.anyio
async def test_monitoring_query_wraps_reports_and_promotions_once(databases: Databases) -> None:
    store, (report,) = await _save_reports(databases, 1)
    refused = _refused(0)
    providers = QueryProviders()
    providers.register(ReportsQueryProvider(databases, store))
    providers.register(PromotionsQueryProvider(cast(Promotions, FakePromotions([refused]))))
    tool = MonitoringQueryTool(providers)

    reports_out = await tool.run(_ctx(), MonitoringQuery(what="reports"))
    promotions_out = await tool.run(_ctx(), MonitoringQuery(what="promotions"))

    stored = await store.get(PROJECT_ID, report.id)
    assert stored is not None
    assert reports_out.content == _wrapped_once([stored])
    assert promotions_out.content == _wrapped_once([refused])


@pytest.mark.anyio
async def test_get_deployments_route(tmp_path: Path, git_env: dict[str, str]) -> None:
    async with (
        started_app(tmp_path / "app_data") as app,
        signed_in(app, "http://127.0.0.1:8765") as client,
    ):
        project_id = await add_local_project(app, tmp_path, git_env)

        resp = await client.get(f"/api/projects/{project_id}/deployments")

    assert resp.status_code == 200
    assert resp.json() == {"environments": [], "decisions": [], "open_prs": []}
