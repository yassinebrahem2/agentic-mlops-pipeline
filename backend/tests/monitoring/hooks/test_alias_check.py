"""Tests for HookAliasVerifier and RequestLinker alias check integration (Task 7, #492)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import pytest

from mlops_agents.events.log import SqliteEventLog
from mlops_agents.ids import new_id
from mlops_agents.monitoring.expectations.contract import (
    ExpectationKey,
    ExpectationState,
)
from mlops_agents.monitoring.hooks.alias_check import HookAliasVerifier
from mlops_agents.monitoring.hooks.service import HooksService
from mlops_agents.monitoring.incidents.contract import SignalDraft, SignalOutcome
from mlops_agents.proposals.handoffs import SqliteHandoffs
from mlops_agents.proposals.payloads import CommandSpec, HandoffPayload
from mlops_agents.storage.databases import Databases
from mlops_agents.tracking.contract import (
    DeploymentRow,
    ModelVersionRow,
    Page,
    RunRow,
    SourceType,
    TrackingQuery,
    TrackingSourceView,
)
from mlops_agents.training.constants import REQUEST_TAG
from mlops_agents.training.contract import TrainingWindow
from mlops_agents.training.fixes import SqliteFixes
from mlops_agents.training.linking import RequestLinker
from mlops_agents.training.requests import SqliteTraining
from tests.proposals.samples import command, model
from tests.proposals.samples import handoff as sample_handoff
from tests.providers.fake_clock import FakeClock


class FakeTracking:
    def __init__(
        self,
        runs: list[RunRow] | None = None,
        versions: list[ModelVersionRow] | None = None,
        deployments: list[DeploymentRow] | None = None,
    ) -> None:
        self.runs: list[RunRow] = runs or []
        self.versions: list[ModelVersionRow] = versions or []
        self.deployments: list[DeploymentRow] = deployments or []

    async def sources(self, project_id: str) -> list[TrackingSourceView]:
        return []

    async def create_source(
        self,
        project_id: str,
        type: SourceType,
        settings: dict[str, Any],
        auth: Literal["none", "credential"],
    ) -> TrackingSourceView:
        raise NotImplementedError

    async def sync(self, project_id: str, source_id: str | None = None) -> None:
        pass

    async def query(self, project_id: str, q: TrackingQuery) -> Page[Any]:
        if q.what == "runs":
            filtered = self.runs
            if q.since is not None:
                filtered = [r for r in filtered if r.start is None or r.start >= q.since]
            total = len(filtered)
            rows = filtered[q.offset : q.offset + q.limit]
            return Page(rows=rows, total=total, truncated=(q.offset + len(rows)) < total)
        elif q.what == "versions":
            filtered = self.versions
            if q.model:
                filtered = [v for v in filtered if v.model == q.model]
            if q.run_key:
                filtered = [v for v in filtered if v.run_key == q.run_key]
            if q.version:
                filtered = [v for v in filtered if v.version == q.version]
            return Page(rows=filtered, total=len(filtered), truncated=False)
        elif q.what == "deployments":
            filtered = self.deployments
            if q.model:
                filtered = [d for d in filtered if d.model == q.model]
            if q.since:
                filtered = [d for d in filtered if d.seen_at >= q.since]
            return Page(rows=filtered, total=len(filtered), truncated=False)
        return Page(rows=[], total=0, truncated=False)


class FakeIncidents:
    def __init__(self) -> None:
        self.signals: list[tuple[str, SignalDraft, datetime]] = []
        self.severities: list[tuple[str, str, str, str, str]] = []

    async def raise_signal(
        self, project_id: str, draft: SignalDraft, seen_at: datetime
    ) -> SignalOutcome:
        self.signals.append((project_id, draft, seen_at))
        return SignalOutcome(
            signal_id="sig_1", incident_id="inc_1", change="opened", severity="medium"
        )

    async def set_severity(
        self, project_id: str, incident_id: str, severity: str, reason: str, by: str
    ) -> None:
        self.severities.append((project_id, incident_id, severity, reason, by))


class FakeExpectations:
    def __init__(self) -> None:
        self.retired: list[tuple[str, ExpectationKey]] = []
        self.seen: list[tuple[str, ExpectationKey, datetime]] = []

    async def states(self, project_id: str) -> list[ExpectationState]:
        return []

    async def note_seen(self, project_id: str, key: ExpectationKey, at: datetime) -> None:
        self.seen.append((project_id, key, at))

    async def retire(self, project_id: str, key: ExpectationKey) -> None:
        self.retired.append((project_id, key))


class FakeWaker:
    async def wake(self, *args: Any, **kwargs: Any) -> None:
        pass


def _spec(deploys: bool = False) -> CommandSpec:
    return CommandSpec.model_validate(
        command(
            kind="train",
            type="command",
            template="python train.py",
            deploys=deploys,
        )
    )


def _make_run(key: str, req_id: str, start: datetime, end: datetime) -> RunRow:
    return RunRow(
        source_id="src_1",
        key=key,
        experiment="exp",
        status="succeeded",
        start=start,
        end=end,
        params={},
        tags={REQUEST_TAG: req_id},
        git_commit="abcdef1234",
        datasets=[],
    )


def _setup_handoff_payload(has_prod_signal: bool = True) -> HandoffPayload:
    m = model(
        name="churn",
        promotion="team_gate",
        environments={"prod": "production", "staging": "staging"},
    )
    if has_prod_signal:
        m["production_signal"] = {"kind": "tracker_alias", "ref": "prod"}
    else:
        m["production_signal"] = None
    return HandoffPayload.model_validate(sample_handoff(models=[m]))


@pytest.mark.asyncio
async def test_model_with_no_production_signal_never_checked(
    databases: Databases,
    events: SqliteEventLog,
    handoffs: SqliteHandoffs,
) -> None:
    project_id = new_id("prj")
    clock = FakeClock()
    training = SqliteTraining(databases, events, clock)

    # Store handoff without production signal
    payload = _setup_handoff_payload(has_prod_signal=False)
    await handoffs.store(project_id, "prp_nosig", payload)

    # Record training request
    req = await training.create_request(
        project_id=project_id,
        request_id="trq_1",
        model="churn",
        hook_id="hok_1",
        mode="train",
        status="started",
        window=TrainingWindow(start=clock.now(), end=clock.now()),
    )
    await training.link(project_id, req.id, run_key="run_123")

    tracking = FakeTracking()
    verifier = HookAliasVerifier(tracking, handoffs, training)

    result = await verifier.check_run(
        project_id, "hok_1", req.id, TrainingWindow(start=clock.now(), end=clock.now())
    )
    assert result.moved is False
    assert result.detail == "Model has no production signal"


@pytest.mark.asyncio
async def test_deploys_false_run_with_production_alias_move_suspends_hook_and_opens_high_incident(
    databases: Databases,
    events: SqliteEventLog,
    handoffs: SqliteHandoffs,
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    t0 = datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC)
    clock = FakeClock(start=t0)
    training = SqliteTraining(databases, events, clock)
    fixes = SqliteFixes(databases, FakeIncidents(), FakeWaker(), events, clock, handoffs)  # type: ignore

    # Store handoff
    payload = _setup_handoff_payload(has_prod_signal=True)
    await handoffs.store(project_id, "prp_h", payload)

    # Register deploys: false hook
    hook = await service.register(project_id, _spec(deploys=False), "train", "churn", "handoff")

    # Start training request
    req = await training.create_request(
        project_id=project_id,
        request_id="trq_2",
        model="churn",
        hook_id=hook.id,
        mode="train",
        status="started",
        window=TrainingWindow(start=t0, end=t0 + timedelta(hours=1)),
    )

    # Tracking records: run produced version "v2", which took the "prod" alias
    run = _make_run("run_xyz", req.id, t0 + timedelta(minutes=5), t0 + timedelta(minutes=25))
    version = ModelVersionRow(
        source_id="src_1",
        model="churn",
        version="v2",
        run_key="run_xyz",
        aliases=["prod"],
        created=t0 + timedelta(minutes=20),
        evaluation="absent",
    )
    deployment = DeploymentRow(
        source_id="src_1",
        model="churn",
        alias="prod",
        from_version="v1",
        to_version="v2",
        seen_at=t0 + timedelta(minutes=22),
    )

    tracking = FakeTracking(runs=[run], versions=[version], deployments=[deployment])
    incidents = FakeIncidents()
    expectations = FakeExpectations()
    verifier = HookAliasVerifier(tracking, handoffs, training, hooks=service)

    linker = RequestLinker(
        training=training,
        fixes=fixes,
        tracking=tracking,
        expectations=expectations,  # type: ignore
        events=events,
        waker=FakeWaker(),  # type: ignore
        hooks=service,
        incidents=incidents,  # type: ignore
        verifier=verifier,
    )

    # Run linker sync pass
    await linker.synced(project_id, now=t0 + timedelta(minutes=30))

    # Verification:
    # 1. Hook suspended
    updated_hook = await service.get(project_id, hook.id)
    assert updated_hook is not None
    assert updated_hook.status == "suspended"
    assert "Production alias 'prod' moved to version 'v2'" in str(updated_hook.suspended_reason)

    # 2. Operational signal raised
    assert len(incidents.signals) == 1
    proj, draft, _seen = incidents.signals[0]
    assert proj == project_id
    assert draft.category == "operational"
    assert draft.kind == "fired_result"
    assert draft.model == "churn"
    assert draft.subject == f"hook:{hook.id}"

    # 3. Incident severity set to high
    assert len(incidents.severities) == 1
    _p, inc_id, sev, reason, by = incidents.severities[0]
    assert inc_id == "inc_1"
    assert sev == "high"
    assert reason == "deploys: false run moved production alias"
    assert by == "system"


@pytest.mark.asyncio
async def test_deploys_false_run_without_move_sets_deploys_verified(
    databases: Databases,
    events: SqliteEventLog,
    handoffs: SqliteHandoffs,
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    t0 = datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC)
    clock = FakeClock(start=t0)
    training = SqliteTraining(databases, events, clock)
    fixes = SqliteFixes(databases, FakeIncidents(), FakeWaker(), events, clock, handoffs)  # type: ignore

    payload = _setup_handoff_payload(has_prod_signal=True)
    await handoffs.store(project_id, "prp_h", payload)

    hook = await service.register(project_id, _spec(deploys=False), "train", "churn", "handoff")

    req = await training.create_request(
        project_id=project_id,
        request_id="trq_4",
        model="churn",
        hook_id=hook.id,
        mode="train",
        status="started",
        window=TrainingWindow(start=t0, end=t0 + timedelta(hours=1)),
    )

    run = _make_run("run_xyz", req.id, t0 + timedelta(minutes=5), t0 + timedelta(minutes=25))
    # Version created with no production alias
    version = ModelVersionRow(
        source_id="src_1",
        model="churn",
        version="v2",
        run_key="run_xyz",
        aliases=[],
        created=t0 + timedelta(minutes=20),
        evaluation="absent",
    )

    tracking = FakeTracking(runs=[run], versions=[version], deployments=[])
    incidents = FakeIncidents()
    expectations = FakeExpectations()
    verifier = HookAliasVerifier(tracking, handoffs, training, hooks=service)

    linker = RequestLinker(
        training=training,
        fixes=fixes,
        tracking=tracking,
        expectations=expectations,  # type: ignore
        events=events,
        waker=FakeWaker(),  # type: ignore
        hooks=service,
        incidents=incidents,  # type: ignore
        verifier=verifier,
    )

    await linker.synced(project_id, now=t0 + timedelta(minutes=30))

    updated_hook = await service.get(project_id, hook.id)
    assert updated_hook is not None
    assert updated_hook.status == "active"
    assert updated_hook.deploys_verified is True
    assert len(incidents.signals) == 0


@pytest.mark.asyncio
async def test_non_production_alias_move_and_production_move_to_another_run_do_not_suspend(
    databases: Databases,
    events: SqliteEventLog,
    handoffs: SqliteHandoffs,
    service: HooksService,
) -> None:
    project_id = new_id("prj")
    t0 = datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC)
    clock = FakeClock(start=t0)
    training = SqliteTraining(databases, events, clock)
    fixes = SqliteFixes(databases, FakeIncidents(), FakeWaker(), events, clock, handoffs)  # type: ignore

    payload = _setup_handoff_payload(has_prod_signal=True)
    await handoffs.store(project_id, "prp_h", payload)

    hook = await service.register(project_id, _spec(deploys=False), "train", "churn", "handoff")

    req = await training.create_request(
        project_id=project_id,
        request_id="trq_4",
        model="churn",
        hook_id=hook.id,
        mode="train",
        status="started",
        window=TrainingWindow(start=t0, end=t0 + timedelta(hours=1)),
    )

    run = _make_run("run_this", req.id, t0 + timedelta(minutes=5), t0 + timedelta(minutes=25))
    # v2 created by this run took non-production alias "staging"
    v2_this = ModelVersionRow(
        source_id="src_1",
        model="churn",
        version="v2",
        run_key="run_this",
        aliases=["staging"],
        created=t0 + timedelta(minutes=20),
        evaluation="absent",
    )
    # v0 created by another run (or manual promotion) took "prod"
    v0_other = ModelVersionRow(
        source_id="src_1",
        model="churn",
        version="v0",
        run_key="run_other",
        aliases=["prod"],
        created=t0 + timedelta(minutes=1),
        evaluation="absent",
    )
    # Deployment moves "staging" to v2 (not prod), and moves "prod" to v0 (not this run)
    dep_staging = DeploymentRow(
        source_id="src_1",
        model="churn",
        alias="staging",
        from_version=None,
        to_version="v2",
        seen_at=t0 + timedelta(minutes=21),
    )
    dep_prod_other = DeploymentRow(
        source_id="src_1",
        model="churn",
        alias="prod",
        from_version="v_prev",
        to_version="v0",
        seen_at=t0 + timedelta(minutes=22),
    )

    tracking = FakeTracking(
        runs=[run],
        versions=[v2_this, v0_other],
        deployments=[dep_staging, dep_prod_other],
    )
    incidents = FakeIncidents()
    expectations = FakeExpectations()
    verifier = HookAliasVerifier(tracking, handoffs, training, hooks=service)

    linker = RequestLinker(
        training=training,
        fixes=fixes,
        tracking=tracking,
        expectations=expectations,  # type: ignore
        events=events,
        waker=FakeWaker(),  # type: ignore
        hooks=service,
        incidents=incidents,  # type: ignore
        verifier=verifier,
    )

    await linker.synced(project_id, now=t0 + timedelta(minutes=30))

    updated_hook = await service.get(project_id, hook.id)
    assert updated_hook is not None
    assert updated_hook.status == "active"
    assert updated_hook.deploys_verified is True
    assert len(incidents.signals) == 0
