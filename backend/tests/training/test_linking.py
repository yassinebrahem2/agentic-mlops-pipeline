"""Tests for RequestLinker and training_request expectation (Task 5, #490)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.expectations.contract import (
    ExpectationKey,
    ExpectationSpec,
    ExpectationState,
)
from mlops_agents.monitoring.hooks.contract import (
    HookCall,
    HookResult,
    HookView,
    PlaceholderResolver,
    RunContext,
)
from mlops_agents.monitoring.incidents.contract import IncidentStatus, IncidentView, WakeLabel
from mlops_agents.proposals.contract import HandoffView
from mlops_agents.proposals.payloads import CommandSpec, HandoffPayload
from mlops_agents.storage.databases import Databases
from mlops_agents.tracking.contract import Page, RunRow, TrackingQuery
from mlops_agents.training.constants import (
    REQUEST_TAG,
    RETRAINING_AGENT,
    TRAINING_REQUEST_DEADLINE_MINUTES,
    TRAINING_REQUEST_GRACE_MINUTES,
)
from mlops_agents.training.fixes import SqliteFixes
from mlops_agents.training.lifecycle import TrainingLifecycle
from mlops_agents.training.linking import RequestLinker
from mlops_agents.training.requests import SqliteTraining
from tests.proposals.samples import handoff
from tests.providers.fake_clock import FakeClock


class FakeHandoffs:
    def __init__(self, current_handoff: HandoffView | None = None) -> None:
        self._current = current_handoff

    async def current(self, project_id: str) -> HandoffView | None:
        return self._current


class FakeWaker:
    def __init__(self) -> None:
        self.wakes: list[dict[str, Any]] = []

    async def wake(
        self,
        project_id: str,
        agent: str,
        label: WakeLabel,
        subject_id: str,
        text: str,
        key: str,
    ) -> None:
        self.wakes.append(
            {
                "project_id": project_id,
                "agent": agent,
                "label": label,
                "text": text,
                "subject_id": subject_id,
                "key": key,
            }
        )


class FakeIncidents:
    def __init__(self) -> None:
        self._incidents: dict[str, IncidentView] = {}

    def add(self, inc: IncidentView) -> None:
        self._incidents[inc.id] = inc

    async def get(self, project_id: str, incident_id: str) -> IncidentView | None:
        return self._incidents.get(incident_id)


class FakeExpectations:
    def __init__(self) -> None:
        self.registered: list[tuple[str, ExpectationSpec]] = []
        self.retired: list[tuple[str, ExpectationKey]] = []
        self._states: list[ExpectationState] = []

    def set_states(self, states: list[ExpectationState]) -> None:
        self._states = states

    async def register(self, project_id: str, spec: ExpectationSpec) -> None:
        self.registered.append((project_id, spec))

    async def retire(self, project_id: str, key: ExpectationKey) -> None:
        self.retired.append((project_id, key))

    async def states(self, project_id: str) -> list[ExpectationState]:
        return list(self._states)


class FakeTracking:
    def __init__(self, runs: list[RunRow] | None = None) -> None:
        self.runs: list[RunRow] = runs or []

    async def query(self, project_id: str, q: TrackingQuery) -> Page[RunRow]:
        if q.what == "runs":
            filtered = self.runs
            if q.since is not None:
                filtered = [r for r in filtered if r.start is None or r.start >= q.since]
            total = len(filtered)
            rows = filtered[q.offset : q.offset + q.limit]
            truncated = (q.offset + len(rows)) < total
            return Page(rows=rows, total=total, truncated=truncated)
        return Page(rows=[], total=0, truncated=False)


class DummyResolver(PlaceholderResolver):
    async def resolve(
        self, hook: HookView, ctx: RunContext, project_id: str | None = None
    ) -> dict[str, str]:
        return {}


def make_handoff(model_name: str = "churn_model") -> HandoffView:
    payload_dict = handoff()
    payload_dict["models"][0]["name"] = model_name
    payload_dict["models"][0]["profile"]["label_maturity_days"] = 1
    payload_dict["data_roles"].append(
        {
            "connection": "db_conn",
            "table": "churn_train",
            "role": "training",
            "id_column": "user_id",
            "time_column": "created_at",
        }
    )
    payload = HandoffPayload.model_validate(payload_dict)
    return HandoffView(
        proposal_id="prop_h1",
        version=1,
        approved=datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC),
        payload=payload,
    )


def make_incident(
    incident_id: str = "inc_001",
    model: str | None = "churn_model",
    status: IncidentStatus = "open",
) -> IncidentView:
    now = datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC)
    return IncidentView(
        id=incident_id,
        category="model_performance",
        subtype="drift",
        severity="medium",
        status=status,
        cause="Feature drift detected in churn dataset",
        cause_key=f"drift:{model}",
        model=model,
        signal_ids=["sig_1"],
        prs=[],
        issues=[],
        held=False,
        merged_into=None,
        started_at=now,
        detected_at=now,
    )


@pytest.fixture
def project_id() -> str:
    return "prj_linking"


async def test_expectation_registered_in_after(databases: Databases, project_id: str) -> None:
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)
    events = SqliteEventLog(databases)
    waker = FakeWaker()
    incidents = FakeIncidents()
    handoffs = FakeHandoffs(make_handoff())
    fixes = SqliteFixes(
        databases,
        incidents,  # type: ignore[arg-type]
        waker,
        events,
        clock,
        handoffs,  # type: ignore[arg-type]
    )
    training = SqliteTraining(databases, events, clock)
    expectations = FakeExpectations()

    lifecycle = TrainingLifecycle(
        training=training,
        fixes=fixes,
        resolver=DummyResolver(),
        clock=clock,
        events=events,
        expectations=expectations,  # type: ignore[arg-type]
    )

    hook = HookView(
        id="h_train",
        name="Train Hook",
        model="churn_model",
        status="active",
        spec=CommandSpec(
            kind="train",
            type="command",
            placeholders=[],
            template="echo 1",
            workflow=None,
            inputs={},
            entry_files=[],
            deploys=False,
        ),
        credential_set=False,
        source="proposal",
        created=now,
    )
    call = HookCall(hook_id="h_train", reason="Retrain", params={"request_id": "trq_001"})
    result = HookResult(ok=True, request_id="trq_001", detail="OK")

    await lifecycle.after(project_id, hook, call, result)

    assert len(expectations.registered) == 1
    p_id, spec = expectations.registered[0]
    assert p_id == project_id
    assert spec.key.kind == "training_request"
    assert spec.key.ref == "trq_001"
    assert spec.key.model == "churn_model"
    assert spec.category == "operational"
    assert spec.cadence is None
    expected_due = now + timedelta(minutes=TRAINING_REQUEST_DEADLINE_MINUTES)
    assert spec.due_at == expected_due
    assert spec.source == "training"
    assert spec.timezone == "UTC"
    assert spec.grace_minutes == TRAINING_REQUEST_GRACE_MINUTES
    assert spec.second_deadline is False
    assert spec.activates_on_pr is None


async def test_tagged_run_links_request_and_wakes_retraining(
    databases: Databases, project_id: str
) -> None:
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)
    events = SqliteEventLog(databases)
    waker = FakeWaker()
    incidents = FakeIncidents()
    inc = make_incident(incident_id="inc_001", model="churn_model")
    incidents.add(inc)
    handoffs = FakeHandoffs(make_handoff())
    fixes = SqliteFixes(
        databases,
        incidents,  # type: ignore[arg-type]
        waker,
        events,
        clock,
        handoffs,  # type: ignore[arg-type]
    )
    training = SqliteTraining(databases, events, clock)
    expectations = FakeExpectations()

    # Create fix request and choose retrain
    fix_view = await fixes.request(project_id, inc.id, "diagnoser", "Drift observed")
    await fixes.choose(
        project_id,
        fix_view.id,
        agent=RETRAINING_AGENT,
        choice="retrain",
        why="Retraining model",
        why_not={"recalibrate": "No score issue", "re_threshold": "Threshold optimal"},
    )

    # Create started training request
    req = await training.create_request(
        project_id=project_id,
        request_id="trq_123",
        model="churn_model",
        hook_id="h_train",
        mode="train",
        status="started",
        fix_id=fix_view.id,
    )

    # Move fix to running
    await fixes.mark_running(project_id, fix_view.id)

    # Create tagged run in tracking
    run = RunRow(
        source_id="src_mlflow",
        key="run_abc_999",
        experiment="churn_exp",
        status="succeeded",
        start=now + timedelta(minutes=5),
        end=now + timedelta(minutes=30),
        params={},
        tags={REQUEST_TAG: req.id},
        git_commit="abcdef1234",
        datasets=["churn_train"],
    )
    tracking = FakeTracking(runs=[run])

    linker = RequestLinker(
        training=training,
        fixes=fixes,
        tracking=tracking,  # type: ignore[arg-type]
        expectations=expectations,  # type: ignore[arg-type]
        events=events,
        waker=waker,
    )

    # Run sync
    await linker.synced(project_id)

    # 1. Training request linked
    req_after = await training.request(project_id, req.id)
    assert req_after is not None
    assert req_after.status == "linked"
    assert req_after.run_key == run.key

    # 2. Expectation retired
    assert len(expectations.retired) == 1
    p_id, retired_key = expectations.retired[0]
    assert p_id == project_id
    assert retired_key == ExpectationKey(kind="training_request", ref=req.id, model="churn_model")

    # 3. Fix status marked done
    fix_after = await fixes.get(project_id, fix_view.id)
    assert fix_after is not None
    assert fix_after.status == "done"

    # 4. Event recorded on incident
    recent_events = await events.page(project_id, limit=50)
    linked_events = [e for e in recent_events if e.type == "training.linked"]
    assert len(linked_events) == 1
    le = linked_events[0]
    assert le.subject_id == inc.id
    assert le.payload["request_id"] == req.id
    assert le.payload["run_key"] == run.key
    assert le.payload["fix_id"] == fix_view.id

    # 5. Retraining woken
    linked_wakes = [w for w in waker.wakes if w["key"] == f"linked:{req.id}"]
    assert len(linked_wakes) == 1
    wake = linked_wakes[0]
    assert wake["agent"] == RETRAINING_AGENT
    assert wake["label"] == "fix_request"
    assert wake["subject_id"] == inc.id
    assert f"Training run linked for fix {fix_view.id}" in wake["text"]


async def test_idempotent_second_sync_changes_nothing(
    databases: Databases, project_id: str
) -> None:
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)
    events = SqliteEventLog(databases)
    waker = FakeWaker()
    incidents = FakeIncidents()
    inc = make_incident(incident_id="inc_002", model="churn_model")
    incidents.add(inc)
    handoffs = FakeHandoffs(make_handoff())
    fixes = SqliteFixes(
        databases,
        incidents,  # type: ignore[arg-type]
        waker,
        events,
        clock,
        handoffs,  # type: ignore[arg-type]
    )
    training = SqliteTraining(databases, events, clock)
    expectations = FakeExpectations()

    fix_view = await fixes.request(project_id, inc.id, "diagnoser", "Drift observed")
    req = await training.create_request(
        project_id=project_id,
        request_id="trq_idem",
        model="churn_model",
        hook_id="h_train",
        mode="train",
        status="started",
        fix_id=fix_view.id,
    )

    run = RunRow(
        source_id="src_mlflow",
        key="run_idem",
        experiment="churn_exp",
        status="succeeded",
        start=now,
        end=now + timedelta(minutes=10),
        params={},
        tags={REQUEST_TAG: req.id},
        git_commit="abc",
        datasets=[],
    )
    tracking = FakeTracking(runs=[run])

    linker = RequestLinker(
        training=training,
        fixes=fixes,
        tracking=tracking,  # type: ignore[arg-type]
        expectations=expectations,  # type: ignore[arg-type]
        events=events,
        waker=waker,
    )

    # First sync
    await linker.synced(project_id)
    retire_count_1 = len(expectations.retired)
    events_count_1 = len(await events.page(project_id, limit=50))
    wakes_count_1 = len(waker.wakes)

    # Second sync
    await linker.synced(project_id)
    assert len(expectations.retired) == retire_count_1
    assert len(await events.page(project_id, limit=50)) == events_count_1
    assert len(waker.wakes) == wakes_count_1


async def test_linking_with_120_preceding_untagged_runs(
    databases: Databases, project_id: str
) -> None:
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)
    events = SqliteEventLog(databases)
    waker = FakeWaker()
    incidents = FakeIncidents()
    inc = make_incident(incident_id="inc_003", model="churn_model")
    incidents.add(inc)
    handoffs = FakeHandoffs(make_handoff())
    fixes = SqliteFixes(
        databases,
        incidents,  # type: ignore[arg-type]
        waker,
        events,
        clock,
        handoffs,  # type: ignore[arg-type]
    )
    training = SqliteTraining(databases, events, clock)
    expectations = FakeExpectations()

    fix_view = await fixes.request(project_id, inc.id, "diagnoser", "Drift observed")
    req = await training.create_request(
        project_id=project_id,
        request_id="trq_paged",
        model="churn_model",
        hook_id="h_train",
        mode="train",
        status="started",
        fix_id=fix_view.id,
    )

    # Generate 120 untagged runs preceding the tagged run
    runs: list[RunRow] = []
    for i in range(120):
        runs.append(
            RunRow(
                source_id="src_mlflow",
                key=f"run_untagged_{i:03d}",
                experiment="other_exp",
                status="succeeded",
                start=now + timedelta(minutes=i),
                end=now + timedelta(minutes=i + 1),
                params={},
                tags={},
                git_commit=None,
                datasets=[],
            )
        )

    # Tagged run comes after the 120 untagged runs
    runs.append(
        RunRow(
            source_id="src_mlflow",
            key="run_target",
            experiment="churn_exp",
            status="succeeded",
            start=now + timedelta(minutes=200),
            end=now + timedelta(minutes=210),
            params={},
            tags={REQUEST_TAG: req.id},
            git_commit=None,
            datasets=[],
        )
    )

    tracking = FakeTracking(runs=runs)
    linker = RequestLinker(
        training=training,
        fixes=fixes,
        tracking=tracking,  # type: ignore[arg-type]
        expectations=expectations,  # type: ignore[arg-type]
        events=events,
        waker=waker,
    )

    await linker.synced(project_id)

    req_after = await training.request(project_id, req.id)
    assert req_after is not None
    assert req_after.status == "linked"
    assert req_after.run_key == "run_target"


async def test_untagged_run_and_wrong_tag_do_not_link(
    databases: Databases, project_id: str
) -> None:
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)
    events = SqliteEventLog(databases)
    waker = FakeWaker()
    incidents = FakeIncidents()
    handoffs = FakeHandoffs(make_handoff())
    fixes = SqliteFixes(
        databases,
        incidents,  # type: ignore[arg-type]
        waker,
        events,
        clock,
        handoffs,  # type: ignore[arg-type]
    )
    training = SqliteTraining(databases, events, clock)
    expectations = FakeExpectations()

    req = await training.create_request(
        project_id=project_id,
        request_id="trq_wait",
        model="churn_model",
        hook_id="h_train",
        mode="train",
        status="started",
    )

    runs = [
        RunRow(
            source_id="src_mlflow",
            key="run_no_tag",
            experiment="exp",
            status="succeeded",
            start=now,
            end=now + timedelta(minutes=1),
            params={},
            tags={},
            git_commit=None,
            datasets=[],
        ),
        RunRow(
            source_id="src_mlflow",
            key="run_diff_tag",
            experiment="exp",
            status="succeeded",
            start=now,
            end=now + timedelta(minutes=2),
            params={},
            tags={REQUEST_TAG: "trq_other_id"},
            git_commit=None,
            datasets=[],
        ),
    ]

    tracking = FakeTracking(runs=runs)
    linker = RequestLinker(
        training=training,
        fixes=fixes,
        tracking=tracking,  # type: ignore[arg-type]
        expectations=expectations,  # type: ignore[arg-type]
        events=events,
        waker=waker,
    )

    await linker.synced(project_id)

    req_after = await training.request(project_id, req.id)
    assert req_after is not None
    assert req_after.status == "started"
    assert len(expectations.retired) == 0
    assert len(waker.wakes) == 0


async def test_silent_expectation_marks_request_silent(
    databases: Databases, project_id: str
) -> None:
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)
    events = SqliteEventLog(databases)
    waker = FakeWaker()
    incidents = FakeIncidents()
    handoffs = FakeHandoffs(make_handoff())
    fixes = SqliteFixes(
        databases,
        incidents,  # type: ignore[arg-type]
        waker,
        events,
        clock,
        handoffs,  # type: ignore[arg-type]
    )
    training = SqliteTraining(databases, events, clock)
    expectations = FakeExpectations()

    req = await training.create_request(
        project_id=project_id,
        request_id="trq_silent",
        model="churn_model",
        hook_id="h_train",
        mode="train",
        status="started",
    )

    # Set expectation state to silent
    expectations.set_states(
        [
            ExpectationState(
                key=ExpectationKey(kind="training_request", ref=req.id, model="churn_model"),
                category="operational",
                active=True,
                status="silent",
                next_deadline=now,
            )
        ]
    )

    tracking = FakeTracking(runs=[])
    linker = RequestLinker(
        training=training,
        fixes=fixes,
        tracking=tracking,  # type: ignore[arg-type]
        expectations=expectations,  # type: ignore[arg-type]
        events=events,
        waker=waker,
    )

    await linker.synced(project_id)

    req_after = await training.request(project_id, req.id)
    assert req_after is not None
    assert req_after.status == "silent"


async def test_request_without_fix_id_links_and_wakes_no_one(
    databases: Databases, project_id: str
) -> None:
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    clock = FakeClock(now)
    events = SqliteEventLog(databases)
    waker = FakeWaker()
    incidents = FakeIncidents()
    handoffs = FakeHandoffs(make_handoff())
    fixes = SqliteFixes(
        databases,
        incidents,  # type: ignore[arg-type]
        waker,
        events,
        clock,
        handoffs,  # type: ignore[arg-type]
    )
    training = SqliteTraining(databases, events, clock)
    expectations = FakeExpectations()

    req = await training.create_request(
        project_id=project_id,
        request_id="trq_no_fix",
        model="churn_model",
        hook_id="h_train",
        mode="train",
        status="started",
        fix_id=None,
    )

    run = RunRow(
        source_id="src_mlflow",
        key="run_standalone",
        experiment="churn_exp",
        status="succeeded",
        start=now,
        end=now + timedelta(minutes=15),
        params={},
        tags={REQUEST_TAG: req.id},
        git_commit=None,
        datasets=[],
    )

    tracking = FakeTracking(runs=[run])
    linker = RequestLinker(
        training=training,
        fixes=fixes,
        tracking=tracking,  # type: ignore[arg-type]
        expectations=expectations,  # type: ignore[arg-type]
        events=events,
        waker=waker,
    )

    await linker.synced(project_id)

    req_after = await training.request(project_id, req.id)
    assert req_after is not None
    assert req_after.status == "linked"
    assert req_after.run_key == run.key

    # Expectation was retired
    assert len(expectations.retired) == 1
    assert expectations.retired[0][1].ref == req.id

    # No one woken
    assert len(waker.wakes) == 0
