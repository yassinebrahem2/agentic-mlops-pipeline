"""Tests for TrackerPlaceholderResolver and TrainingLifecycle (Task 3, #488)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from unittest.mock import AsyncMock

import pytest

from mlops_agents.agents.approvals.authority import Authority
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.ids import new_id
from mlops_agents.monitoring.hooks.contract import (
    HookCall,
    HookRefused,
    HookResult,
    HookView,
    MissingPlaceholder,
    RunContext,
)
from mlops_agents.monitoring.hooks.placeholders import TrackerPlaceholderResolver
from mlops_agents.monitoring.hooks.service import HooksService
from mlops_agents.monitoring.incidents.contract import (
    HoldScope,
    IncidentStatus,
    IncidentView,
    SignalView,
)
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import CommandSpec, HandoffPayload
from mlops_agents.storage.databases import Databases
from mlops_agents.tracking.contract import ModelSummary, Page, TrackingQuery
from mlops_agents.training.contract import (
    GuardRefusal,
    GuardResult,
    TrainingWindow,
)
from mlops_agents.training.fixes import SqliteFixes
from mlops_agents.training.lifecycle import TrainingLifecycle
from mlops_agents.training.requests import SqliteTraining
from tests.conftest import memory_project_secrets
from tests.monitoring.hooks.conftest import FakeRunner
from tests.proposals.samples import handoff
from tests.providers.fake_clock import FakeClock


class FakeHandoffs:
    def __init__(self, current_handoff: HandoffView | None = None) -> None:
        self._current = current_handoff

    async def current(self, project_id: str) -> HandoffView | None:
        return self._current


def make_handoff_view(
    model_name: str = "churn_model",
    maturity_days: int = 7,
    training_table: str = "churn_train",
) -> HandoffView:
    payload_dict = handoff()
    payload_dict["models"][0]["name"] = model_name
    payload_dict["models"][0]["profile"]["label_maturity_days"] = maturity_days
    payload_dict["data_roles"].append(
        {
            "connection": "db_conn",
            "table": training_table,
            "role": "training",
            "id_column": "user_id",
            "time_column": "created_at",
        }
    )
    payload = HandoffPayload.model_validate(payload_dict)
    return HandoffView(
        proposal_id="prop_guards",
        version=1,
        approved=datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC),
        payload=payload,
    )


class FakeTracking:
    def __init__(self, models: dict[str, str | None] | None = None) -> None:
        self.models: dict[str, str | None] = models or {}
        self.query_calls: list[tuple[str, TrackingQuery]] = []

    async def query(self, project_id: str, q: TrackingQuery) -> Page[ModelSummary]:
        self.query_calls.append((project_id, q))
        if q.what == "models" and q.model:
            if q.model in self.models:
                summary = ModelSummary(
                    source_id="src-1",
                    name=q.model,
                    production_version=self.models[q.model],
                    latest_version="v2.0",
                    versions=2,
                )
                return Page[ModelSummary](rows=[summary], total=1, truncated=False)
            return Page[ModelSummary](rows=[], total=0, truncated=False)
        return Page[ModelSummary](rows=[], total=0, truncated=False)


class FakeWaker:
    def __init__(self) -> None:
        self.wakes: list[dict[str, Any]] = []

    async def wake(
        self,
        project_id: str,
        agent: str,
        label: str,
        text: str = "",
        subject_id: str | None = None,
        key: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.wakes.append(
            {
                "project_id": project_id,
                "agent": agent,
                "text": text,
                "label": label,
                "subject_id": subject_id,
                "key": key,
            }
        )


class FakeIncidents:
    def __init__(self, incidents: dict[str, IncidentView] | None = None) -> None:
        self._incidents: dict[str, IncidentView] = incidents or {}
        self.held_ranges: list[HoldScope] = []

    def add(self, incident: IncidentView) -> None:
        self._incidents[incident.id] = incident

    async def get(self, project_id: str, incident_id: str) -> IncidentView | None:
        if incident_id not in self._incidents:
            now = datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC)
            return IncidentView(
                id=incident_id,
                category="model_performance",
                subtype="drift",
                severity="medium",
                status="open",
                cause="drift",
                cause_key="drift:churn_model",
                model="churn_model",
                signal_ids=[],
                prs=[],
                issues=[],
                held=False,
                started_at=now,
                detected_at=now,
            )
        return self._incidents.get(incident_id)

    async def list(
        self,
        project_id: str,
        status: list[IncidentStatus] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[IncidentView]:
        rows = list(self._incidents.values())
        return Page[IncidentView](rows=rows, total=len(rows), truncated=False)

    async def holds(self, project_id: str, model: str | None = None) -> list[HoldScope]:
        return self.held_ranges

    async def signals(self, project_id: str, incident_id: str) -> list[SignalView]:
        return []


def _make_hook(
    hook_id: str,
    kind: Literal["train", "evaluate", "deploy", "rollback", "rerun", "hold", "fall_back"],
    placeholders: list[str],
    model: str | None = "churn_model",
    type_: Literal["command", "ci_dispatch"] = "command",
) -> HookView:
    template = "echo " + " ".join(f"{{{p}}}" for p in placeholders) if placeholders else "echo ok"
    return HookView(
        id=hook_id,
        name=f"Hook {hook_id}",
        model=model,
        spec=CommandSpec(
            kind=kind,
            type=type_,
            template=template,
            workflow=None,
            inputs={},
            placeholders=placeholders,  # type: ignore[arg-type]
            deploys=False,
            entry_files=[],
        ),
        status="active",
        credential_set=False,
        source="proposal",
        created=datetime.now(UTC),
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC))


@pytest.fixture
def events(databases: Databases) -> SqliteEventLog:
    return SqliteEventLog(databases)


@pytest.fixture
def tracking() -> FakeTracking:
    return FakeTracking({"churn_model": "v1.0.0"})


@pytest.fixture
def resolver(tracking: FakeTracking) -> TrackerPlaceholderResolver:
    return TrackerPlaceholderResolver(tracking=tracking)  # type: ignore[arg-type]


@pytest.fixture
def training_store(
    databases: Databases, events: SqliteEventLog, clock: FakeClock
) -> SqliteTraining:
    return SqliteTraining(databases=databases, events=events, clock=clock)


@pytest.fixture
def fixes_store(databases: Databases, events: SqliteEventLog, clock: FakeClock) -> SqliteFixes:
    return SqliteFixes(
        databases=databases,
        incidents=FakeIncidents(),  # type: ignore[arg-type]
        waker=FakeWaker(),  # type: ignore[arg-type]
        events=events,
        clock=clock,
        handoffs=FakeHandoffs(make_handoff_view()),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Placeholder resolver tests
# ---------------------------------------------------------------------------


async def test_resolver_all_placeholders(
    resolver: TrackerPlaceholderResolver, tracking: FakeTracking
) -> None:
    hook = _make_hook(
        "h1",
        "train",
        [
            "model",
            "champion_version",
            "request_id",
            "mode",
            "window_start",
            "window_end",
            "candidate_version",
            "threshold",
        ],
    )
    ctx = RunContext(
        model="churn_model",
        request_id="trq_12345",
        mode="train",
        window=TrainingWindow(
            start=datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC),
            end=datetime(2026, 10, 15, 0, 0, 0, tzinfo=UTC),
        ),
        candidate_version="v2.0.0",
        threshold=0.85,
    )

    resolved = await resolver.resolve(hook, ctx, project_id="prj_1")
    assert resolved["model"] == "churn_model"
    assert resolved["champion_version"] == "v1.0.0"
    assert resolved["request_id"] == "trq_12345"
    assert resolved["mode"] == "train"
    assert resolved["candidate_version"] == "v2.0.0"
    assert resolved["threshold"] == "0.85"
    assert "2026-10-01" in resolved["window_start"]
    assert "2026-10-15" in resolved["window_end"]


async def test_resolver_missing_placeholder_raises(resolver: TrackerPlaceholderResolver) -> None:
    hook = _make_hook("h1", "train", ["candidate_version"])
    ctx = RunContext(
        model="churn_model",
        candidate_version=None,
    )
    with pytest.raises(MissingPlaceholder) as exc_info:
        await resolver.resolve(hook, ctx)
    assert exc_info.value.placeholder == "candidate_version"


async def test_resolver_champion_version_runtime_and_only_queried_when_declared(
    resolver: TrackerPlaceholderResolver, tracking: FakeTracking
) -> None:
    # 1. Not declared -> tracking not queried
    hook_no_champ = _make_hook("h1", "train", ["model"])
    ctx = RunContext(model="churn_model")
    await resolver.resolve(hook_no_champ, ctx, project_id="prj_1")
    assert len(tracking.query_calls) == 0

    # 2. Declared -> tracking queried at runtime
    hook_with_champ = _make_hook("h2", "train", ["champion_version"])
    tracking.models["churn_model"] = "v1.0.0"
    res1 = await resolver.resolve(hook_with_champ, ctx, project_id="prj_1")
    assert res1["champion_version"] == "v1.0.0"
    assert len(tracking.query_calls) == 1

    # Alias moves between two runs:
    tracking.models["churn_model"] = "v2.5.0"
    res2 = await resolver.resolve(hook_with_champ, ctx, project_id="prj_1")
    assert res2["champion_version"] == "v2.5.0"
    assert len(tracking.query_calls) == 2


async def test_resolver_invalid_format_refused(
    resolver: TrackerPlaceholderResolver, tracking: FakeTracking
) -> None:
    hook = _make_hook("h1", "train", ["model"])
    ctx_bad_model = RunContext(model="churn; rm -rf /")
    with pytest.raises(HookRefused):
        await resolver.resolve(hook, ctx_bad_model)

    hook_ver = _make_hook("h2", "deploy", ["candidate_version"])
    ctx_bad_ver = RunContext(model="churn", candidate_version="v 1.0")
    with pytest.raises(HookRefused):
        await resolver.resolve(hook_ver, ctx_bad_ver)


# ---------------------------------------------------------------------------
# TrainingLifecycle tests
# ---------------------------------------------------------------------------


async def test_train_run_creates_trq_records_event_and_moves_fix_to_running(
    databases: Databases,
    training_store: SqliteTraining,
    fixes_store: SqliteFixes,
    resolver: TrackerPlaceholderResolver,
    events: SqliteEventLog,
    clock: FakeClock,
) -> None:
    project_id = "prj_1"
    # Create an initial fix and move it to chosen
    fix = await fixes_store.request(project_id, "inc_1", "retraining", "some evidence")
    await fixes_store.choose(
        project_id,
        fix.id,
        "retraining",
        "retrain",
        "Need retraining",
        {"recalibrate": "not sufficient", "re_threshold": "drift too large"},
    )
    chosen_fix = await fixes_store.get(project_id, fix.id)
    assert chosen_fix is not None
    assert chosen_fix.status == "chosen"

    lifecycle = TrainingLifecycle(
        training=training_store,
        fixes=fixes_store,
        resolver=resolver,
        clock=clock,
        events=events,
    )

    hook = _make_hook(
        "hook-train-1", "train", ["model", "request_id", "window_start", "window_end"]
    )
    call = HookCall(
        hook_id=hook.id,
        reason="Model drift detected",
        incident_id="inc_1",
        fix_id=fix.id,
        params={
            "window_start": "2026-10-01T00:00:00Z",
            "window_end": "2026-10-15T00:00:00Z",
        },
    )

    updated_call = await lifecycle.before(project_id, hook, call)
    req_id = updated_call.params.get("request_id")
    assert req_id is not None
    assert req_id.startswith("trq_")
    assert updated_call.params["model"] == "churn_model"

    # Verify request was stored
    stored_req = await training_store.request(project_id, req_id)
    assert stored_req is not None
    assert stored_req.status == "started"
    assert stored_req.mode == "train"
    assert stored_req.fix_id == fix.id
    assert stored_req.hook_id == hook.id

    # Verify fix was moved to running
    running_fix = await fixes_store.get(project_id, fix.id)
    assert running_fix is not None
    assert running_fix.status == "running"

    # Verify training.requested event recorded
    ev_history = await events.history(project_id, types=["training.requested"])
    assert len(ev_history) == 1
    ev = ev_history[0]
    assert ev.type == "training.requested"
    assert ev.source == "training"
    assert ev.subject_id == fix.id
    assert ev.payload["request_id"] == req_id
    assert ev.payload["hook_id"] == hook.id


async def test_train_run_without_window_refused(
    training_store: SqliteTraining,
    fixes_store: SqliteFixes,
    resolver: TrackerPlaceholderResolver,
    events: SqliteEventLog,
    clock: FakeClock,
) -> None:
    lifecycle = TrainingLifecycle(
        training=training_store,
        fixes=fixes_store,
        resolver=resolver,
        clock=clock,
        events=events,
    )
    hook = _make_hook("hook-train-1", "train", ["model"])
    call = HookCall(hook_id=hook.id, reason="No window", params={})

    with pytest.raises(HookRefused, match="Window is required"):
        await lifecycle.before("prj_1", hook, call)

    # Verify no trq created
    requests = await training_store.requests("prj_1", "churn_model")
    assert len(requests) == 0


async def test_train_run_guard_refusal_gives_one_hook_refused_with_all_refusals(
    databases: Databases,
    training_store: SqliteTraining,
    fixes_store: SqliteFixes,
    resolver: TrackerPlaceholderResolver,
    events: SqliteEventLog,
    clock: FakeClock,
) -> None:
    # Mock fixes.guards to return multiple refusals
    fixes_mock = AsyncMock(spec=SqliteFixes)
    fixes_mock.guards.return_value = GuardResult(
        ok=False,
        refusals=[
            GuardRefusal(code="data_held", detail="Data table t1 is held"),
            GuardRefusal(code="labels_immature", detail="Labels not mature yet"),
        ],
    )

    lifecycle = TrainingLifecycle(
        training=training_store,
        fixes=fixes_mock,
        resolver=resolver,
        clock=clock,
        events=events,
    )
    hook = _make_hook("hook-train-1", "train", ["model", "window_start", "window_end"])
    call = HookCall(
        hook_id=hook.id,
        reason="Guard test",
        params={"window_start": "2026-10-01T00:00:00Z", "window_end": "2026-10-15T00:00:00Z"},
    )

    with pytest.raises(HookRefused) as exc_info:
        await lifecycle.before("prj_1", hook, call)

    msg = str(exc_info.value)
    assert "data_held" in msg
    assert "labels_immature" in msg
    assert "Data table t1 is held" in msg
    assert "Labels not mature yet" in msg

    # No requests created
    reqs = await training_store.requests("prj_1", "churn_model")
    assert len(reqs) == 0


async def test_train_run_missing_placeholder_leaves_no_trq(
    training_store: SqliteTraining,
    fixes_store: SqliteFixes,
    resolver: TrackerPlaceholderResolver,
    events: SqliteEventLog,
    clock: FakeClock,
) -> None:
    lifecycle = TrainingLifecycle(
        training=training_store,
        fixes=fixes_store,
        resolver=resolver,
        clock=clock,
        events=events,
    )
    # Hook declares candidate_version, but call does not provide it
    hook = _make_hook(
        "hook-train-1", "train", ["model", "candidate_version", "window_start", "window_end"]
    )
    call = HookCall(
        hook_id=hook.id,
        reason="Missing placeholder test",
        params={"window_start": "2026-10-01T00:00:00Z", "window_end": "2026-10-15T00:00:00Z"},
    )

    with pytest.raises(MissingPlaceholder):
        await lifecycle.before("prj_1", hook, call)

    # Ensure no request row in DB
    reqs = await training_store.requests("prj_1", "churn_model")
    assert len(reqs) == 0


async def test_other_hook_deploy_resolves_candidate_version(
    training_store: SqliteTraining,
    fixes_store: SqliteFixes,
    resolver: TrackerPlaceholderResolver,
    events: SqliteEventLog,
    clock: FakeClock,
) -> None:
    lifecycle = TrainingLifecycle(
        training=training_store,
        fixes=fixes_store,
        resolver=resolver,
        clock=clock,
        events=events,
    )
    hook = _make_hook("hook-deploy-1", "deploy", ["model", "candidate_version"])
    call = HookCall(
        hook_id=hook.id,
        reason="Deploy promotion",
        params={"candidate_version": "v3.0.0"},
    )

    updated_call = await lifecycle.before("prj_1", hook, call)
    assert updated_call.params["candidate_version"] == "v3.0.0"
    assert updated_call.params["model"] == "churn_model"

    # Non-training hook does NOT create a trq row
    reqs = await training_store.requests("prj_1", "churn_model")
    assert len(reqs) == 0


async def test_invalid_window_start_not_a_date_refused(
    training_store: SqliteTraining,
    fixes_store: SqliteFixes,
    resolver: TrackerPlaceholderResolver,
    events: SqliteEventLog,
    clock: FakeClock,
) -> None:
    lifecycle = TrainingLifecycle(
        training=training_store,
        fixes=fixes_store,
        resolver=resolver,
        clock=clock,
        events=events,
    )
    hook = _make_hook("hook-train-1", "train", ["model", "window_start", "window_end"])
    call = HookCall(
        hook_id=hook.id,
        reason="Invalid date",
        params={"window_start": "yesterday", "window_end": "2026-10-15T00:00:00Z"},
    )
    with pytest.raises(HookRefused, match="Invalid window date format"):
        await lifecycle.before("prj_1", hook, call)


async def test_invalid_model_or_version_with_space_or_semicolon_refused(
    training_store: SqliteTraining,
    fixes_store: SqliteFixes,
    resolver: TrackerPlaceholderResolver,
    events: SqliteEventLog,
    clock: FakeClock,
) -> None:
    lifecycle = TrainingLifecycle(
        training=training_store,
        fixes=fixes_store,
        resolver=resolver,
        clock=clock,
        events=events,
    )

    # Model with space
    hook_space = _make_hook("hook-1", "train", ["model"], model="churn model")
    call = HookCall(
        hook_id=hook_space.id,
        reason="Model space",
        params={"window_start": "2026-10-01T00:00:00Z", "window_end": "2026-10-15T00:00:00Z"},
    )
    with pytest.raises(HookRefused, match="Invalid model name format"):
        await lifecycle.before("prj_1", hook_space, call)

    # Model with semicolon
    hook_semi = _make_hook("hook-2", "train", ["model"], model="churn;drop")
    with pytest.raises(HookRefused, match="Invalid model name format"):
        await lifecycle.before("prj_1", hook_semi, call)

    # Candidate version with space or semicolon
    hook_valid = _make_hook("hook-3", "deploy", ["candidate_version"])
    call_bad_ver = HookCall(
        hook_id=hook_valid.id,
        reason="Bad ver",
        params={"candidate_version": "v1.0; rm"},
    )
    with pytest.raises(HookRefused, match="Invalid candidate_version format"):
        await lifecycle.before("prj_1", hook_valid, call_bad_ver)


async def test_declared_placeholder_missing_nothing_runs_no_runner_call(
    databases: Databases,
    training_store: SqliteTraining,
    fixes_store: SqliteFixes,
    resolver: TrackerPlaceholderResolver,
    events: SqliteEventLog,
    clock: FakeClock,
) -> None:
    project_id = "prj_1"
    secrets = memory_project_secrets(databases)
    service = HooksService(databases=databases, secrets=secrets, events=events, clock=clock)

    runner = FakeRunner("command", HookResult(ok=True, detail="ok"))
    service.add_runner(runner)

    lifecycle = TrainingLifecycle(
        training=training_store,
        fixes=fixes_store,
        resolver=resolver,
        clock=clock,
        events=events,
    )
    service.add_lifecycle(lifecycle)

    # Register train hook declaring candidate_version which is missing from call params
    registered = await service.register(
        project_id,
        CommandSpec(
            kind="train",
            type="command",
            template="python train.py --cand {candidate_version}",
            workflow=None,
            inputs={},
            placeholders=["candidate_version", "window_start", "window_end"],
            deploys=False,
            entry_files=[],
        ),
        name="MissingCandTrainHook",
        model="churn_model",
        source="proposal",
    )

    call = HookCall(
        hook_id=registered.id,
        reason="Run missing placeholder",
        params={
            "window_start": "2026-10-01T00:00:00Z",
            "window_end": "2026-10-15T00:00:00Z",
        },
    )
    authority = Authority(basis="approval", ref="apr-1", reason="Approved", evidence=[])

    with pytest.raises(MissingPlaceholder):
        await service.run(project_id, call, authority)

    # Double records no call!
    assert len(runner.calls) == 0


async def test_rollback_hook_declaring_request_id_refused(
    training_store: SqliteTraining,
    fixes_store: SqliteFixes,
    resolver: TrackerPlaceholderResolver,
    events: SqliteEventLog,
    clock: FakeClock,
) -> None:
    lifecycle = TrainingLifecycle(
        training=training_store,
        fixes=fixes_store,
        resolver=resolver,
        clock=clock,
        events=events,
    )
    hook = _make_hook("hook-rollback-1", "rollback", ["request_id"])
    call = HookCall(hook_id=hook.id, reason="Rollback")

    with pytest.raises(MissingPlaceholder) as exc_info:
        await lifecycle.before("prj_1", hook, call)
    assert exc_info.value.placeholder == "request_id"


async def test_after_failed_result_marks_request_failed(
    training_store: SqliteTraining,
    fixes_store: SqliteFixes,
    resolver: TrackerPlaceholderResolver,
    events: SqliteEventLog,
    clock: FakeClock,
) -> None:
    project_id = "prj_1"
    req = await training_store.create_request(
        project_id=project_id,
        request_id="trq_test_after",
        model="churn_model",
        hook_id="h1",
        mode="train",
        status="started",
    )
    lifecycle = TrainingLifecycle(
        training=training_store,
        fixes=fixes_store,
        resolver=resolver,
        clock=clock,
        events=events,
    )

    hook = _make_hook("h1", "train", ["model"])
    call = HookCall(hook_id="h1", reason="Testing after", params={"request_id": req.id})
    result = HookResult(ok=False, detail="Command timed out", request_id=req.id)

    await lifecycle.after(project_id, hook, call, result)

    updated_req = await training_store.request(project_id, req.id)
    assert updated_req is not None
    assert updated_req.status == "failed"


async def test_end_to_end_hook_run_with_lifecycle(
    databases: Databases,
    training_store: SqliteTraining,
    fixes_store: SqliteFixes,
    resolver: TrackerPlaceholderResolver,
    events: SqliteEventLog,
    clock: FakeClock,
) -> None:
    project_id = "prj_1"
    secrets = memory_project_secrets(databases)
    service = HooksService(databases=databases, secrets=secrets, events=events, clock=clock)

    runner = FakeRunner("command", HookResult(ok=True, detail="ok"))
    service.add_runner(runner)

    lifecycle = TrainingLifecycle(
        training=training_store,
        fixes=fixes_store,
        resolver=resolver,
        clock=clock,
        events=events,
    )
    service.add_lifecycle(lifecycle)

    # Register train hook
    registered = await service.register(
        project_id,
        CommandSpec(
            kind="train",
            type="command",
            template="python train.py --model {model} --req {request_id}",
            workflow=None,
            inputs={},
            placeholders=["model", "request_id", "window_start", "window_end"],
            deploys=False,
            entry_files=[],
        ),
        name="TrainHook",
        model="churn_model",
        source="proposal",
    )

    call = HookCall(
        hook_id=registered.id,
        reason="Run train",
        params={
            "window_start": "2026-10-01T00:00:00Z",
            "window_end": "2026-10-15T00:00:00Z",
        },
    )
    authority = Authority(basis="approval", ref="apr-1", reason="Approved by user", evidence=[])
    res = await service.run(project_id, call, authority)

    assert res.ok is True
    assert res.request_id is not None
    assert len(runner.calls) == 1
    called_params = runner.calls[0][2]
    assert called_params["model"] == "churn_model"
    assert called_params["request_id"] == res.request_id

    # Verify trq exists
    trq = await training_store.request(project_id, res.request_id)
    assert trq is not None
    assert trq.status == "started"


async def test_non_finite_threshold_refused(
    training_store: SqliteTraining,
    fixes_store: SqliteFixes,
    clock: FakeClock,
    events: SqliteEventLog,
    resolver: TrackerPlaceholderResolver,
) -> None:
    lifecycle = TrainingLifecycle(
        training=training_store,
        fixes=fixes_store,
        resolver=resolver,
        clock=clock,
        events=events,
    )
    hook = HookView(
        id="hook-eval",
        name="eval",
        model="churn_model",
        spec=CommandSpec(
            kind="evaluate",
            type="command",
            template="echo eval",
            workflow=None,
            inputs={},
            placeholders=["threshold"],
            deploys=False,
            entry_files=[],
        ),
        status="active",
        credential_set=False,
        source="proposal",
        created=datetime.now(UTC),
    )

    # NaN in call params
    call_nan = HookCall(
        hook_id="hook-eval",
        reason="test",
        params={
            "threshold": "nan",
            "window_start": "2026-10-01T00:00:00Z",
            "window_end": "2026-10-15T00:00:00Z",
        },
    )
    with pytest.raises(HookRefused, match="Invalid threshold format"):
        await lifecycle.before("prj_1", hook, call_nan)

    # Inf in call params
    call_inf = HookCall(
        hook_id="hook-eval",
        reason="test",
        params={
            "threshold": "inf",
            "window_start": "2026-10-01T00:00:00Z",
            "window_end": "2026-10-15T00:00:00Z",
        },
    )
    with pytest.raises(HookRefused, match="Invalid threshold format"):
        await lifecycle.before("prj_1", hook, call_inf)

    # NaN directly in resolver
    ctx = RunContext(model="churn_model", threshold=float("nan"))
    with pytest.raises(HookRefused, match="Refused non-finite threshold"):
        await resolver.resolve(hook, ctx)


async def test_non_training_hook_drops_agent_request_id_and_after_does_not_fail_request(
    databases: Databases,
    training_store: SqliteTraining,
    fixes_store: SqliteFixes,
    clock: FakeClock,
    events: SqliteEventLog,
    resolver: TrackerPlaceholderResolver,
) -> None:
    project_id = "prj_safe"
    _ = await databases.project(project_id)

    # Existing training request in started state
    target_trq = new_id("trq")
    await training_store.create_request(
        project_id=project_id,
        request_id=target_trq,
        model="churn_model",
        hook_id="train-hk",
        mode="train",
        status="started",
    )

    lifecycle = TrainingLifecycle(
        training=training_store,
        fixes=fixes_store,
        resolver=resolver,
        clock=clock,
        events=events,
    )

    rollback_hook = HookView(
        id="hook-rb",
        name="rollback",
        model="churn_model",
        spec=CommandSpec(
            kind="rollback",
            type="command",
            template="echo rollback",
            workflow=None,
            inputs={},
            placeholders=["model"],
            deploys=False,
            entry_files=[],
        ),
        status="active",
        credential_set=False,
        source="proposal",
        created=datetime.now(UTC),
    )

    # Agent supplies a request_id for another request
    call = HookCall(
        hook_id="hook-rb",
        reason="Rollback incident",
        params={"request_id": target_trq},
    )

    updated_call = await lifecycle.before(project_id, rollback_hook, call)
    # request_id must be dropped in before() for non-training hooks
    assert "request_id" not in updated_call.params

    # Simulate execution failure
    failed_result = HookResult(ok=False, detail="Rollback failed", request_id=None)
    await lifecycle.after(project_id, rollback_hook, updated_call, failed_result)

    # Verify target request was NOT marked failed
    req = await training_store.request(project_id, target_trq)
    assert req is not None
    assert req.status == "started"
