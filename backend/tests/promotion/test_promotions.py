"""Tests for SqlitePromotions (Task 3, #497)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

import pytest

from mlops_agents.clock import now_iso
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.hooks.contract import Authority, HookCall, HookResult, HookView
from mlops_agents.promotion.contract import (
    EvaluationPayload,
    EvaluationReader,
    EvaluationView,
    PromotionPayload,
    ReportDraft,
    ReportField,
    Reports,
    ReportView,
)
from mlops_agents.promotion.promotions import SqlitePromotions
from mlops_agents.proposals.contract import Handoffs, HandoffView
from mlops_agents.proposals.payloads import CommandSpec, HandoffPayload
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import promotions, proposals
from mlops_agents.tracking.contract import (
    EvaluationRecord,
    ModelSummary,
    Page,
    RunRow,
    Tracking,
    TrackingQuery,
)
from tests.proposals.samples import handoff as sample_handoff
from tests.proposals.samples import model as sample_model

PROJECT_ID = "prj_promotiontest"


class FakeTracking(Tracking):
    def __init__(self) -> None:
        self.models: list[ModelSummary] = []
        self.runs: list[RunRow] = []

    async def sources(self, project_id: str) -> list[Any]:
        return []

    async def create_source(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def sync(self, project_id: str, source_id: str | None = None) -> None:
        pass

    async def query(self, project_id: str, q: TrackingQuery) -> Page[Any]:
        if q.what == "models":
            rows = [m for m in self.models if q.model is None or m.name == q.model]
            return Page(rows=rows, total=len(rows), truncated=False)
        elif q.what == "runs":
            rows = [r for r in self.runs if q.run_key is None or r.key == q.run_key]
            return Page(rows=rows, total=len(rows), truncated=False)
        return Page(rows=[], total=0, truncated=False)


class FakeHandoffs(Handoffs):
    def __init__(self, current_handoff: HandoffView | None = None) -> None:
        self.current_handoff = current_handoff

    def set_current(self, current_handoff: HandoffView | None) -> None:
        self.current_handoff = current_handoff

    def add_observer(self, observer: Any) -> None:
        pass

    async def current(self, project_id: str) -> HandoffView | None:
        return self.current_handoff

    async def store(self, project_id: str, proposal_id: str, payload: HandoffPayload) -> int:
        return 1


class FakeEvaluationReader(EvaluationReader):
    def __init__(self) -> None:
        self.evaluations: dict[tuple[str, str], EvaluationView] = {}

    async def for_candidate(
        self, project_id: str, model: str, version: str
    ) -> EvaluationView | None:
        return self.evaluations.get((model, version))

    async def latest(self, project_id: str, model: str) -> EvaluationView | None:
        for (m, _), ev in self.evaluations.items():
            if m == model:
                return ev
        return None


class FakeReports(Reports):
    def __init__(self) -> None:
        self.reports: dict[str, ReportView] = {}

    async def save(self, project_id: str, draft: ReportDraft, author: str) -> ReportView:
        raise NotImplementedError

    async def get(self, project_id: str, report_id: str) -> ReportView | None:
        return self.reports.get(report_id)

    async def for_candidate(self, project_id: str, model: str, version: str) -> ReportView | None:
        for r in self.reports.values():
            if r.draft.model == model and r.draft.candidate_version == version:
                return r
        return None


class FakeHooks:
    def __init__(self) -> None:
        self.hooks: dict[str, HookView] = {}
        self.run_calls: list[tuple[HookCall, Authority]] = []
        self.next_result: HookResult = HookResult(ok=True, detail="Deployed successfully")

    async def get(self, project_id: str, hook_id: str) -> HookView | None:
        return self.hooks.get(hook_id)

    async def run(self, project_id: str, call: HookCall, authority: Authority) -> HookResult:
        self.run_calls.append((call, authority))
        return self.next_result


def _make_handoff(
    model: str = "churn_model",
    mode: str = "person",
) -> HandoffView:
    raw = sample_handoff(models=[sample_model(model, promotion=mode)])
    payload = HandoffPayload.model_validate(raw)
    return HandoffView(
        proposal_id="prp_handoff_1",
        version=1,
        approved=datetime.now(UTC),
        payload=payload,
    )


def _make_hook(
    hook_id: str = "chk_deploy_1",
    model: str = "churn_model",
    kind: str = "deploy",
    status: str = "active",
) -> HookView:
    spec = CommandSpec(
        kind=kind,  # pyright: ignore[reportArgumentType]
        type="command",
        template="./deploy.sh {candidate_version}",
        workflow=None,
        inputs={},
        placeholders=["candidate_version"],
        deploys=True,
        entry_files=["deploy.sh"],
    )
    return HookView(
        id=hook_id,
        name="deploy_hook",
        model=model,
        spec=spec,
        status=status,  # pyright: ignore[reportArgumentType]
        credential_set=True,
        pins=[],
        approved_commit="abc1234",
        deploys_verified=True,
        source="handoff",
        created=datetime.now(UTC),
    )


def _make_eval(
    model: str = "churn_model",
    version: str = "2.0.0",
    champion_version: str | None = "1.0.0",
    run_key: str = "run_2",
    commit: str = "commit_1",
    gate_version: str = "v1",
) -> EvaluationView:
    record = EvaluationRecord(
        source_id="src_test",
        model=model,
        version=version,
        run_key=run_key,
        schema_version=1,
        payload={},
        fetched_at=datetime.now(UTC),
    )
    payload = EvaluationPayload(
        schema_version=1,
        evaluation_commit=commit,
        gate_config_version=gate_version,
        champion_version=champion_version,
        decision="promote",
        reasons=["All metrics improved"],
    )
    return EvaluationView(record=record, payload=payload, not_computed={})


def _make_report(
    report_id: str = "rep_1",
    model: str = "churn_model",
    candidate_version: str = "2.0.0",
) -> ReportView:
    draft = ReportDraft(
        model=model,
        candidate_version=candidate_version,
        assessments={"fix_chosen": ReportField(value="retrained model")},
    )
    return ReportView(
        id=report_id,
        draft=draft,
        evaluation=None,
        created=datetime.now(UTC),
        author="agent_retraining",
    )


class SetupEnv(TypedDict):
    databases: Databases
    events: SqliteEventLog
    tracking: FakeTracking
    handoffs: FakeHandoffs
    reader: FakeEvaluationReader
    reports: FakeReports
    hooks: FakeHooks
    service: SqlitePromotions


@pytest.fixture
async def setup_env(databases: Databases) -> SetupEnv:
    await databases.project(PROJECT_ID)
    events = SqliteEventLog(databases)
    tracking = FakeTracking()
    handoffs = FakeHandoffs(_make_handoff())
    reader = FakeEvaluationReader()
    reports_svc = FakeReports()
    hooks_svc = FakeHooks()

    promotions_svc = SqlitePromotions(
        databases=databases,
        tracking=tracking,
        handoffs=handoffs,
        reader=reader,
        reports=reports_svc,
        hooks=hooks_svc,  # pyright: ignore[reportArgumentType]
        events=events,
    )

    return {
        "databases": databases,
        "events": events,
        "tracking": tracking,
        "handoffs": handoffs,
        "reader": reader,
        "reports": reports_svc,
        "hooks": hooks_svc,
        "service": promotions_svc,
    }


@pytest.mark.asyncio
async def test_validate_returns_each_refusal_on_its_own(setup_env: SetupEnv) -> None:
    env = setup_env
    svc: SqlitePromotions = env["service"]
    tracking: FakeTracking = env["tracking"]
    reader: FakeEvaluationReader = env["reader"]
    reports_svc: FakeReports = env["reports"]
    hooks_svc: FakeHooks = env["hooks"]

    # Seed baseline valid state
    reports_svc.reports["rep_1"] = _make_report("rep_1", "churn_model", "2.0.0")
    hooks_svc.hooks["chk_deploy_1"] = _make_hook("chk_deploy_1", "churn_model", "deploy", "active")
    reader.evaluations[("churn_model", "2.0.0")] = _make_eval(
        "churn_model", "2.0.0", "1.0.0", "run_2"
    )
    reader.evaluations[("churn_model", "1.0.0")] = _make_eval("churn_model", "1.0.0", None, "run_1")

    now = datetime.now(UTC)
    tracking.models = [
        ModelSummary(
            source_id="src_1",
            name="churn_model",
            production_version="1.0.0",
            latest_version="2.0.0",
            versions=2,
        )
    ]
    tracking.runs = [
        RunRow(
            source_id="src_1",
            key="run_1",
            experiment="exp",
            status="succeeded",
            start=now - timedelta(days=2),
            end=now - timedelta(days=2),
            params={},
            tags={},
            git_commit="c1",
            datasets=[],
        ),
        RunRow(
            source_id="src_1",
            key="run_2",
            experiment="exp",
            status="succeeded",
            start=now - timedelta(hours=1),
            end=now - timedelta(hours=1),
            params={},
            tags={},
            git_commit="c2",
            datasets=[],
        ),
    ]

    good_payload = PromotionPayload(
        model="churn_model",
        report_id="rep_1",
        candidate_version="2.0.0",
        champion_version="1.0.0",
        deploy_hook_id="chk_deploy_1",
    )

    # 0. Baseline passes
    assert await svc.validate(PROJECT_ID, good_payload) == []

    # 1. Report missing or mismatch
    bad_report = good_payload.model_copy(update={"report_id": "nonexistent_rep"})
    refusals = await svc.validate(PROJECT_ID, bad_report)
    assert len(refusals) == 1
    assert "Report" in refusals[0] and "not exist" in refusals[0]

    reports_svc.reports["rep_wrong_model"] = _make_report("rep_wrong_model", "other_model", "2.0.0")
    bad_report_model = good_payload.model_copy(update={"report_id": "rep_wrong_model"})
    refusals = await svc.validate(PROJECT_ID, bad_report_model)
    assert len(refusals) == 1
    assert "other_model" in refusals[0]

    # 2. Handoff not person mode
    env["handoffs"].set_current(_make_handoff("churn_model", mode="team_gate"))
    refusals = await svc.validate(PROJECT_ID, good_payload)
    assert any("team_gate" in r and "person" in r for r in refusals)
    env["handoffs"].set_current(_make_handoff("churn_model", mode="person"))

    # 3. Deploy hook issues
    bad_hook_id = good_payload.model_copy(update={"deploy_hook_id": "nonexistent_hook"})
    refusals = await svc.validate(PROJECT_ID, bad_hook_id)
    assert any("not exist" in r for r in refusals)

    hooks_svc.hooks["chk_train"] = _make_hook(
        "chk_train", "churn_model", kind="train", status="active"
    )
    bad_hook_kind = good_payload.model_copy(update={"deploy_hook_id": "chk_train"})
    refusals = await svc.validate(PROJECT_ID, bad_hook_kind)
    assert any("train" in r and "deploy" in r for r in refusals)

    hooks_svc.hooks["chk_other_model"] = _make_hook(
        "chk_other_model", "other_model", kind="deploy", status="active"
    )
    bad_hook_model = good_payload.model_copy(update={"deploy_hook_id": "chk_other_model"})
    refusals = await svc.validate(PROJECT_ID, bad_hook_model)
    assert any("other_model" in r for r in refusals)

    hooks_svc.hooks["chk_suspended"] = _make_hook(
        "chk_suspended", "churn_model", kind="deploy", status="suspended"
    )
    bad_hook_status = good_payload.model_copy(update={"deploy_hook_id": "chk_suspended"})
    refusals = await svc.validate(PROJECT_ID, bad_hook_status)
    assert any("suspended" in r for r in refusals)

    # 4. Candidate has no evaluation
    del reader.evaluations[("churn_model", "2.0.0")]
    refusals = await svc.validate(PROJECT_ID, good_payload)
    assert any("no evaluation record" in r for r in refusals)
    reader.evaluations[("churn_model", "2.0.0")] = _make_eval(
        "churn_model", "2.0.0", "1.0.0", "run_2"
    )

    # 5. Candidate evaluation not fresh (no end or stale)
    tracking.runs = [r for r in tracking.runs if r.key != "run_2"]
    refusals = await svc.validate(PROJECT_ID, good_payload)
    assert any("no known end time" in r for r in refusals)

    tracking.runs.append(
        RunRow(
            source_id="src_1",
            key="run_2",
            experiment="exp",
            status="succeeded",
            start=now - timedelta(days=10),
            end=now - timedelta(days=10),
            params={},
            tags={},
            git_commit="c2",
            datasets=[],
        )
    )
    refusals = await svc.validate(PROJECT_ID, good_payload)
    assert any("exceeding maximum age" in r for r in refusals)

    # restore run_2
    tracking.runs = [r for r in tracking.runs if r.key != "run_2"]
    tracking.runs.append(
        RunRow(
            source_id="src_1",
            key="run_2",
            experiment="exp",
            status="succeeded",
            start=now - timedelta(hours=1),
            end=now - timedelta(hours=1),
            params={},
            tags={},
            git_commit="c2",
            datasets=[],
        )
    )

    # 6. Champion version mismatch with current prod
    tracking.models = [
        ModelSummary(
            source_id="src_1",
            name="churn_model",
            production_version="0.9.0",
            latest_version="2.0.0",
            versions=2,
        )
    ]
    refusals = await svc.validate(PROJECT_ID, good_payload)
    assert any("0.9.0" in r for r in refusals)
    tracking.models = [
        ModelSummary(
            source_id="src_1",
            name="churn_model",
            production_version="1.0.0",
            latest_version="2.0.0",
            versions=2,
        )
    ]

    # 7. Champion evaluation missing or not comparable
    del reader.evaluations[("churn_model", "1.0.0")]
    refusals = await svc.validate(PROJECT_ID, good_payload)
    assert any("Champion version '1.0.0' has no evaluation record" in r for r in refusals)

    reader.evaluations[("churn_model", "1.0.0")] = _make_eval(
        "churn_model", "1.0.0", None, "run_1", commit="diff_commit", gate_version="v1"
    )
    refusals = await svc.validate(PROJECT_ID, good_payload)
    assert any("not comparable" in r for r in refusals)


@pytest.mark.asyncio
async def test_apply_good_payload_runs_deploy_hook_and_records_applied(setup_env: SetupEnv) -> None:
    env = setup_env
    svc: SqlitePromotions = env["service"]
    tracking: FakeTracking = env["tracking"]
    reader: FakeEvaluationReader = env["reader"]
    reports_svc: FakeReports = env["reports"]
    hooks_svc: FakeHooks = env["hooks"]
    databases: Databases = env["databases"]
    events: SqliteEventLog = env["events"]

    now = datetime.now(UTC)
    reports_svc.reports["rep_1"] = _make_report("rep_1", "churn_model", "2.0.0")
    hooks_svc.hooks["chk_deploy_1"] = _make_hook("chk_deploy_1", "churn_model", "deploy", "active")
    reader.evaluations[("churn_model", "2.0.0")] = _make_eval(
        "churn_model", "2.0.0", "1.0.0", "run_2"
    )
    reader.evaluations[("churn_model", "1.0.0")] = _make_eval("churn_model", "1.0.0", None, "run_1")

    tracking.models = [
        ModelSummary(
            source_id="src_1",
            name="churn_model",
            production_version="1.0.0",
            latest_version="2.0.0",
            versions=2,
        )
    ]
    tracking.runs = [
        RunRow(
            source_id="src_1",
            key="run_1",
            experiment="exp",
            status="succeeded",
            start=now - timedelta(days=2),
            end=now - timedelta(days=2),
            params={},
            tags={},
            git_commit="c1",
            datasets=[],
        ),
        RunRow(
            source_id="src_1",
            key="run_2",
            experiment="exp",
            status="succeeded",
            start=now - timedelta(hours=1),
            end=now - timedelta(hours=1),
            params={},
            tags={},
            git_commit="c2",
            datasets=[],
        ),
    ]

    payload = PromotionPayload(
        model="churn_model",
        report_id="rep_1",
        candidate_version="2.0.0",
        champion_version="1.0.0",
        deploy_hook_id="chk_deploy_1",
    )

    # Insert a proposal with approval_id in proposals table
    engine = await databases.project(PROJECT_ID)
    async with engine.begin() as conn:
        await conn.execute(
            proposals.insert().values(
                id="prp_promo_1",
                kind="promotion",
                key="churn_model",
                payload=payload.model_dump_json(),
                status="approved",
                agent="retraining",
                approval_id="apr_1",
                turn_id="turn_1",
                tool_call_id="call_1",
                created_at=now_iso(),
            )
        )

    res = await svc.apply(PROJECT_ID, "prp_promo_1", payload)
    assert res.applied == ["promotion"]
    assert res.failed == {}

    # Check hook call
    assert len(hooks_svc.run_calls) == 1
    call, auth = hooks_svc.run_calls[0]
    assert call.hook_id == "chk_deploy_1"
    assert call.params == {"candidate_version": "2.0.0"}
    assert auth.basis == "approval"
    assert auth.ref == "prp_promo_1"

    # Check row in promotions table
    hist = await svc.history(PROJECT_ID)
    assert len(hist) == 1
    p_view = hist[0]
    assert p_view.status == "applied"
    assert p_view.decided_by == "person"
    assert p_view.applied_at is not None

    # Check event
    evs = await events.page(PROJECT_ID, types=["promotion.applied"])
    assert len(evs) == 1
    assert evs[0].subject_id == p_view.id


@pytest.mark.asyncio
async def test_apply_stale_or_moved_champion_refuses_without_hook_run(setup_env: SetupEnv) -> None:
    env = setup_env
    svc: SqlitePromotions = env["service"]
    tracking: FakeTracking = env["tracking"]
    reader: FakeEvaluationReader = env["reader"]
    reports_svc: FakeReports = env["reports"]
    hooks_svc: FakeHooks = env["hooks"]
    databases: Databases = env["databases"]
    events: SqliteEventLog = env["events"]

    now = datetime.now(UTC)
    reports_svc.reports["rep_1"] = _make_report("rep_1", "churn_model", "2.0.0")
    hooks_svc.hooks["chk_deploy_1"] = _make_hook("chk_deploy_1", "churn_model", "deploy", "active")
    reader.evaluations[("churn_model", "2.0.0")] = _make_eval(
        "churn_model", "2.0.0", "1.0.0", "run_2"
    )
    reader.evaluations[("churn_model", "1.0.0")] = _make_eval("churn_model", "1.0.0", None, "run_1")

    # Champion moved to 1.1.0 meanwhile!
    tracking.models = [
        ModelSummary(
            source_id="src_1",
            name="churn_model",
            production_version="1.1.0",
            latest_version="2.0.0",
            versions=2,
        )
    ]
    tracking.runs = [
        RunRow(
            source_id="src_1",
            key="run_1",
            experiment="exp",
            status="succeeded",
            start=now - timedelta(days=2),
            end=now - timedelta(days=2),
            params={},
            tags={},
            git_commit="c1",
            datasets=[],
        ),
        RunRow(
            source_id="src_1",
            key="run_2",
            experiment="exp",
            status="succeeded",
            start=now - timedelta(hours=1),
            end=now - timedelta(hours=1),
            params={},
            tags={},
            git_commit="c2",
            datasets=[],
        ),
    ]

    payload = PromotionPayload(
        model="churn_model",
        report_id="rep_1",
        candidate_version="2.0.0",
        champion_version="1.0.0",
        deploy_hook_id="chk_deploy_1",
    )

    engine = await databases.project(PROJECT_ID)
    async with engine.begin() as conn:
        await conn.execute(
            proposals.insert().values(
                id="prp_promo_moved",
                kind="promotion",
                key="churn_model",
                payload=payload.model_dump_json(),
                status="approved",
                agent="retraining",
                approval_id="apr_2",
                turn_id="turn_2",
                tool_call_id="call_2",
                created_at=now_iso(),
            )
        )

    res = await svc.apply(PROJECT_ID, "prp_promo_moved", payload)
    assert res.applied == []
    assert "promotion" in res.failed
    assert len(hooks_svc.run_calls) == 0

    hist = await svc.history(PROJECT_ID)
    assert len(hist) == 1
    assert hist[0].status == "refused"

    evs = await events.page(PROJECT_ID, types=["promotion.refused"])
    assert len(evs) == 1


@pytest.mark.asyncio
async def test_apply_failed_hook_run_records_refused(setup_env: SetupEnv) -> None:
    env = setup_env
    svc: SqlitePromotions = env["service"]
    tracking: FakeTracking = env["tracking"]
    reader: FakeEvaluationReader = env["reader"]
    reports_svc: FakeReports = env["reports"]
    hooks_svc: FakeHooks = env["hooks"]
    databases: Databases = env["databases"]

    now = datetime.now(UTC)
    reports_svc.reports["rep_1"] = _make_report("rep_1", "churn_model", "2.0.0")
    hooks_svc.hooks["chk_deploy_1"] = _make_hook("chk_deploy_1", "churn_model", "deploy", "active")
    hooks_svc.next_result = HookResult(ok=False, detail="Container exit status 1")
    reader.evaluations[("churn_model", "2.0.0")] = _make_eval(
        "churn_model", "2.0.0", "1.0.0", "run_2"
    )
    reader.evaluations[("churn_model", "1.0.0")] = _make_eval("churn_model", "1.0.0", None, "run_1")

    tracking.models = [
        ModelSummary(
            source_id="src_1",
            name="churn_model",
            production_version="1.0.0",
            latest_version="2.0.0",
            versions=2,
        )
    ]
    tracking.runs = [
        RunRow(
            source_id="src_1",
            key="run_1",
            experiment="exp",
            status="succeeded",
            start=now - timedelta(days=2),
            end=now - timedelta(days=2),
            params={},
            tags={},
            git_commit="c1",
            datasets=[],
        ),
        RunRow(
            source_id="src_1",
            key="run_2",
            experiment="exp",
            status="succeeded",
            start=now - timedelta(hours=1),
            end=now - timedelta(hours=1),
            params={},
            tags={},
            git_commit="c2",
            datasets=[],
        ),
    ]

    payload = PromotionPayload(
        model="churn_model",
        report_id="rep_1",
        candidate_version="2.0.0",
        champion_version="1.0.0",
        deploy_hook_id="chk_deploy_1",
    )

    engine = await databases.project(PROJECT_ID)
    async with engine.begin() as conn:
        await conn.execute(
            proposals.insert().values(
                id="prp_promo_failed_hook",
                kind="promotion",
                key="churn_model",
                payload=payload.model_dump_json(),
                status="approved",
                agent="retraining",
                approval_id="apr_3",
                turn_id="turn_3",
                tool_call_id="call_3",
                created_at=now_iso(),
            )
        )

    res = await svc.apply(PROJECT_ID, "prp_promo_failed_hook", payload)
    assert res.applied == []
    assert res.failed == {"promotion": "Container exit status 1"}

    hist = await svc.history(PROJECT_ID)
    assert hist[0].status == "refused"
    assert hist[0].refusals == ["Container exit status 1"]


@pytest.mark.asyncio
async def test_apply_idempotent_and_recovery_interrupted(setup_env: SetupEnv) -> None:
    env = setup_env
    svc: SqlitePromotions = env["service"]
    tracking: FakeTracking = env["tracking"]
    reader: FakeEvaluationReader = env["reader"]
    reports_svc: FakeReports = env["reports"]
    hooks_svc: FakeHooks = env["hooks"]
    databases: Databases = env["databases"]

    now = datetime.now(UTC)
    reports_svc.reports["rep_1"] = _make_report("rep_1", "churn_model", "2.0.0")
    hooks_svc.hooks["chk_deploy_1"] = _make_hook("chk_deploy_1", "churn_model", "deploy", "active")
    reader.evaluations[("churn_model", "2.0.0")] = _make_eval(
        "churn_model", "2.0.0", "1.0.0", "run_2"
    )
    reader.evaluations[("churn_model", "1.0.0")] = _make_eval("churn_model", "1.0.0", None, "run_1")

    tracking.models = [
        ModelSummary(
            source_id="src_1",
            name="churn_model",
            production_version="1.0.0",
            latest_version="2.0.0",
            versions=2,
        )
    ]
    tracking.runs = [
        RunRow(
            source_id="src_1",
            key="run_1",
            experiment="exp",
            status="succeeded",
            start=now - timedelta(days=2),
            end=now - timedelta(days=2),
            params={},
            tags={},
            git_commit="c1",
            datasets=[],
        ),
        RunRow(
            source_id="src_1",
            key="run_2",
            experiment="exp",
            status="succeeded",
            start=now - timedelta(hours=1),
            end=now - timedelta(hours=1),
            params={},
            tags={},
            git_commit="c2",
            datasets=[],
        ),
    ]

    payload = PromotionPayload(
        model="churn_model",
        report_id="rep_1",
        candidate_version="2.0.0",
        champion_version="1.0.0",
        deploy_hook_id="chk_deploy_1",
    )

    engine = await databases.project(PROJECT_ID)
    async with engine.begin() as conn:
        await conn.execute(
            proposals.insert().values(
                id="prp_promo_idem",
                kind="promotion",
                key="churn_model",
                payload=payload.model_dump_json(),
                status="approved",
                agent="retraining",
                approval_id="apr_4",
                turn_id="turn_4",
                tool_call_id="call_4",
                created_at=now_iso(),
            )
        )

    res1 = await svc.apply(PROJECT_ID, "prp_promo_idem", payload)
    assert res1.applied == ["promotion"]
    assert len(hooks_svc.run_calls) == 1

    # Second apply returns stored outcome without running hook again
    res2 = await svc.apply(PROJECT_ID, "prp_promo_idem", payload)
    assert res2.applied == ["promotion"]
    assert len(hooks_svc.run_calls) == 1

    # Recovery apply for an unfinished proposal stuck at 'proposed'
    async with engine.begin() as conn:
        await conn.execute(
            promotions.insert().values(
                id="prm_crashed",
                model="churn_model",
                candidate_version="2.0.0",
                champion_version="1.0.0",
                mode="person",
                decided_by="person",
                status="proposed",
                report_id="rep_1",
                refusals="[]",
                created=now_iso(),
                applied_at=None,
                proposal_id="prp_crashed",
            )
        )

    res_rec = await svc.apply(PROJECT_ID, "prp_crashed", payload)
    assert res_rec.applied == []
    assert "interrupted; check the Deployments page" in res_rec.failed["promotion"]
    # Hook should still have run only once
    assert len(hooks_svc.run_calls) == 1


@pytest.mark.asyncio
async def test_apply_without_approval_records_agent_bypass(setup_env: SetupEnv) -> None:
    env = setup_env
    svc: SqlitePromotions = env["service"]
    tracking: FakeTracking = env["tracking"]
    reader: FakeEvaluationReader = env["reader"]
    reports_svc: FakeReports = env["reports"]
    hooks_svc: FakeHooks = env["hooks"]
    databases: Databases = env["databases"]

    now = datetime.now(UTC)
    reports_svc.reports["rep_1"] = _make_report("rep_1", "churn_model", "2.0.0")
    hooks_svc.hooks["chk_deploy_1"] = _make_hook("chk_deploy_1", "churn_model", "deploy", "active")
    reader.evaluations[("churn_model", "2.0.0")] = _make_eval(
        "churn_model", "2.0.0", "1.0.0", "run_2"
    )
    reader.evaluations[("churn_model", "1.0.0")] = _make_eval("churn_model", "1.0.0", None, "run_1")

    tracking.models = [
        ModelSummary(
            source_id="src_1",
            name="churn_model",
            production_version="1.0.0",
            latest_version="2.0.0",
            versions=2,
        )
    ]
    tracking.runs = [
        RunRow(
            source_id="src_1",
            key="run_1",
            experiment="exp",
            status="succeeded",
            start=now - timedelta(days=2),
            end=now - timedelta(days=2),
            params={},
            tags={},
            git_commit="c1",
            datasets=[],
        ),
        RunRow(
            source_id="src_1",
            key="run_2",
            experiment="exp",
            status="succeeded",
            start=now - timedelta(hours=1),
            end=now - timedelta(hours=1),
            params={},
            tags={},
            git_commit="c2",
            datasets=[],
        ),
    ]

    payload = PromotionPayload(
        model="churn_model",
        report_id="rep_1",
        candidate_version="2.0.0",
        champion_version="1.0.0",
        deploy_hook_id="chk_deploy_1",
    )

    # Insert proposal with NO approval_id (agent bypass)
    engine = await databases.project(PROJECT_ID)
    async with engine.begin() as conn:
        await conn.execute(
            proposals.insert().values(
                id="prp_promo_bypass",
                kind="promotion",
                key="churn_model",
                payload=payload.model_dump_json(),
                status="approved",
                agent="retraining",
                approval_id=None,
                turn_id="turn_5",
                tool_call_id="call_5",
                created_at=now_iso(),
            )
        )

    res = await svc.apply(PROJECT_ID, "prp_promo_bypass", payload)
    assert res.applied == ["promotion"]

    hist = await svc.history(PROJECT_ID)
    promo = next(p for p in hist if p.id.startswith("prm"))
    assert promo.decided_by == "agent_bypass"


@pytest.mark.asyncio
async def test_record_rejection(setup_env: SetupEnv) -> None:
    env = setup_env
    svc: SqlitePromotions = env["service"]

    payload = PromotionPayload(
        model="churn_model",
        report_id="rep_1",
        candidate_version="2.0.0",
        champion_version="1.0.0",
        deploy_hook_id="chk_deploy_1",
    )

    await svc.record_rejection(PROJECT_ID, "prp_rejected", payload)

    hist = await svc.history(PROJECT_ID)
    assert len(hist) == 1
    assert hist[0].status == "rejected"
    assert hist[0].decided_by == "person"

    # Calling again does not insert a second row
    await svc.record_rejection(PROJECT_ID, "prp_rejected", payload)
    hist2 = await svc.history(PROJECT_ID)
    assert len(hist2) == 1
