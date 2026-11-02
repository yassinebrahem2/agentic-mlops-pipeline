"""Tests for PostDeployObserver (Task 4, #498)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from mlops_agents.api.services import Services
from mlops_agents.clock import to_iso
from mlops_agents.monitoring.incidents.contract import AgentWaker, WakeLabel
from mlops_agents.promotion.contract import Promotions, PromotionView
from mlops_agents.promotion.post_deploy import CURSOR_KEY, PostDeployObserver
from mlops_agents.proposals.contract import Handoffs, HandoffView
from mlops_agents.proposals.payloads import HandoffPayload
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import project_settings
from mlops_agents.tracking.contract import DeploymentRow, Page, Tracking, TrackingQuery
from tests.api.conftest import started_app
from tests.proposals.samples import handoff as sample_handoff
from tests.proposals.samples import model as sample_model

PROJECT_ID = "prj_postdeploytest"


class RecordingWaker(AgentWaker):
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
                "subject_id": subject_id,
                "text": text,
                "key": key,
            }
        )


class FakeTracking(Tracking):
    def __init__(self) -> None:
        self.deployments: list[DeploymentRow] = []

    async def sources(self, project_id: str) -> list[Any]:
        return []

    async def create_source(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def sync(self, project_id: str, source_id: str | None = None) -> None:
        pass

    async def query(self, project_id: str, q: TrackingQuery) -> Page[Any]:
        if q.what == "deployments":
            rows = list(self.deployments)
            if q.model:
                rows = [r for r in rows if r.model == q.model]
            if q.since:
                since_dt = (
                    q.since.astimezone(UTC) if q.since.tzinfo else q.since.replace(tzinfo=UTC)
                )
                rows = [
                    r
                    for r in rows
                    if (
                        r.seen_at.astimezone(UTC)
                        if r.seen_at.tzinfo
                        else r.seen_at.replace(tzinfo=UTC)
                    )
                    >= since_dt
                ]
            rows.sort(
                key=lambda r: (
                    r.seen_at.astimezone(UTC) if r.seen_at.tzinfo else r.seen_at.replace(tzinfo=UTC)
                ),
                reverse=True,
            )
            return Page(rows=rows, total=len(rows), truncated=False)
        return Page(rows=[], total=0, truncated=False)


class FakeHandoffs(Handoffs):
    def __init__(self, handoff: HandoffView | None = None) -> None:
        self.current_handoff = handoff

    async def current(self, project_id: str) -> HandoffView | None:
        return self.current_handoff

    async def store(self, project_id: str, proposal_id: str, payload: HandoffPayload) -> int:
        return 1

    def add_observer(self, observer: Any) -> None:
        pass


class FakePromotions(Promotions):
    def __init__(self, promotions: list[PromotionView] | None = None) -> None:
        self.items = list(promotions or [])

    async def validate(self, project_id: str, payload: Any) -> list[str]:
        return []

    async def history(self, project_id: str, model: str | None = None) -> list[PromotionView]:
        res = self.items
        if model is not None:
            res = [p for p in res if p.model == model]
        return res


def make_handoff(
    model_name: str = "fraud_detection", environments: dict[str, str] | None = None
) -> HandoffView:
    envs = environments or {"production": "production", "staging": "staging"}
    prod_alias = next(
        (alias for alias, env in envs.items() if env == "production"),
        "production",
    )
    m = sample_model(
        model_name,
        environments=envs,
        production_signal={"kind": "tracker_alias", "ref": prod_alias},
    )
    raw = sample_handoff(models=[m])
    payload = HandoffPayload.model_validate(raw)
    return HandoffView(
        proposal_id="prp_handoff_1",
        version=1,
        approved=datetime.now(UTC),
        payload=payload,
    )


@pytest.mark.anyio
async def test_first_run_after_startup_does_not_wake_for_older_moves(databases: Databases) -> None:
    await databases.project(PROJECT_ID)
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    older_seen = now - timedelta(hours=1)

    tracking = FakeTracking()
    tracking.deployments.append(
        DeploymentRow(
            source_id="src_1",
            model="fraud_detection",
            alias="production",
            from_version="v1",
            to_version="v2",
            seen_at=older_seen,
        )
    )

    handoffs = FakeHandoffs(make_handoff())
    promotions = FakePromotions()
    waker = RecordingWaker()

    observer = PostDeployObserver(
        databases=databases,
        tracking=tracking,
        handoffs=handoffs,
        promotions=promotions,
        waker=waker,
    )

    await observer.synced(PROJECT_ID, now=now)

    # First run must NOT wake anyone for older moves
    assert len(waker.wakes) == 0

    # Cursor must be initialized to now
    engine = await databases.project(PROJECT_ID)
    async with engine.connect() as conn:
        cursor_val = (
            await conn.execute(
                select(project_settings.c.value).where(project_settings.c.key == CURSOR_KEY)
            )
        ).scalar_one_or_none()
    assert cursor_val == to_iso(now)


@pytest.mark.anyio
async def test_alias_moved_to_production_alias_wakes_once_with_versions(
    databases: Databases,
) -> None:
    await databases.project(PROJECT_ID)
    t0 = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    t2 = t0 + timedelta(minutes=10)

    # Pre-set cursor to t0
    observer_init = PostDeployObserver(
        databases=databases,
        tracking=FakeTracking(),
        handoffs=FakeHandoffs(make_handoff()),
        promotions=FakePromotions(),  # type: ignore[arg-type]
        waker=RecordingWaker(),
    )
    await observer_init.synced(PROJECT_ID, now=t0)

    # Add promotion in history
    prm = PromotionView(
        id="prm_test123",
        model="fraud_detection",
        candidate_version="v2",
        champion_version="v1",
        mode="person",
        decided_by="person",
        status="applied",
        created=t0,
        applied_at=t1,
    )
    promotions = FakePromotions([prm])

    tracking = FakeTracking()
    tracking.deployments.append(
        DeploymentRow(
            source_id="src_1",
            model="fraud_detection",
            alias="production",
            from_version="v1",
            to_version="v2",
            seen_at=t1,
        )
    )

    waker = RecordingWaker()
    observer = PostDeployObserver(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        promotions=promotions,
        waker=waker,
    )

    await observer.synced(PROJECT_ID, now=t2)

    assert len(waker.wakes) == 1
    wake = waker.wakes[0]
    assert wake["agent"] == "deployment"
    assert wake["label"] == "post_deploy"
    assert wake["subject_id"] == "prm_test123"
    assert wake["key"] == f"fraud_detection:production:v1:v2:{to_iso(t1)}"
    assert '<data source="tracking">\nfraud_detection\n</data>' in wake["text"]
    assert '<data source="tracking">\nv1\n</data>' in wake["text"]
    assert '<data source="tracking">\nv2\n</data>' in wake["text"]
    assert "Compare the evaluation record with the live label-based results." in wake["text"]

    # Cursor should now be updated to t1
    engine = await databases.project(PROJECT_ID)
    async with engine.connect() as conn:
        cursor_val = (
            await conn.execute(
                select(project_settings.c.value).where(project_settings.c.key == CURSOR_KEY)
            )
        ).scalar_one_or_none()
    assert cursor_val == to_iso(t1)


@pytest.mark.anyio
async def test_baseline_deployment_is_skipped_and_advances_cursor(
    databases: Databases,
) -> None:
    await databases.project(PROJECT_ID)
    t0 = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    t2 = t0 + timedelta(minutes=10)

    # Pre-set cursor to t0
    observer_init = PostDeployObserver(
        databases=databases,
        tracking=FakeTracking(),
        handoffs=FakeHandoffs(make_handoff()),
        promotions=FakePromotions(),
        waker=RecordingWaker(),
    )
    await observer_init.synced(PROJECT_ID, now=t0)

    tracking = FakeTracking()
    tracking.deployments.append(
        DeploymentRow(
            source_id="src_1",
            model="fraud_detection",
            alias="production",
            from_version=None,
            to_version="v1",
            seen_at=t1,
            baseline=True,
        )
    )

    waker = RecordingWaker()
    observer = PostDeployObserver(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        promotions=FakePromotions(),
        waker=waker,
    )

    await observer.synced(PROJECT_ID, now=t2)

    # Baseline deployment must NOT wake anyone!
    assert len(waker.wakes) == 0

    # But cursor must still advance past t1!
    engine = await databases.project(PROJECT_ID)
    async with engine.connect() as conn:
        cursor_val = (
            await conn.execute(
                select(project_settings.c.value).where(project_settings.c.key == CURSOR_KEY)
            )
        ).scalar_one_or_none()
    assert cursor_val == to_iso(t1)


@pytest.mark.anyio
async def test_subject_id_is_model_when_no_promotion_exists(databases: Databases) -> None:
    await databases.project(PROJECT_ID)
    t0 = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)

    observer = PostDeployObserver(
        databases=databases,
        tracking=FakeTracking(),
        handoffs=FakeHandoffs(make_handoff()),
        promotions=FakePromotions(),  # type: ignore[arg-type]
        waker=RecordingWaker(),
    )
    await observer.synced(PROJECT_ID, now=t0)

    tracking = FakeTracking()
    tracking.deployments.append(
        DeploymentRow(
            source_id="src_1",
            model="fraud_detection",
            alias="production",
            from_version=None,
            to_version="v1",
            seen_at=t1,
        )
    )

    waker = RecordingWaker()
    observer = PostDeployObserver(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        promotions=FakePromotions(),  # type: ignore[arg-type]
        waker=waker,
    )

    await observer.synced(PROJECT_ID, now=t1 + timedelta(seconds=1))

    assert len(waker.wakes) == 1
    wake = waker.wakes[0]
    assert wake["subject_id"] == "fraud_detection"
    assert "Old version: none" in wake["text"]


@pytest.mark.anyio
async def test_non_production_alias_and_removed_alias_wake_no_one(databases: Databases) -> None:
    await databases.project(PROJECT_ID)
    t0 = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=2)
    t2 = t0 + timedelta(minutes=4)

    observer = PostDeployObserver(
        databases=databases,
        tracking=FakeTracking(),
        handoffs=FakeHandoffs(make_handoff()),
        promotions=FakePromotions(),  # type: ignore[arg-type]
        waker=RecordingWaker(),
    )
    await observer.synced(PROJECT_ID, now=t0)

    tracking = FakeTracking()
    # Non-production alias: staging
    tracking.deployments.append(
        DeploymentRow(
            source_id="src_1",
            model="fraud_detection",
            alias="staging",
            from_version="v1",
            to_version="v2",
            seen_at=t1,
        )
    )
    # Removed alias: to_version is None
    tracking.deployments.append(
        DeploymentRow(
            source_id="src_1",
            model="fraud_detection",
            alias="production",
            from_version="v2",
            to_version=None,
            seen_at=t2,
        )
    )

    waker = RecordingWaker()
    observer = PostDeployObserver(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        promotions=FakePromotions(),  # type: ignore[arg-type]
        waker=waker,
    )

    await observer.synced(PROJECT_ID, now=t2 + timedelta(seconds=1))

    # Neither should trigger a wake
    assert len(waker.wakes) == 0

    # Cursor should advance to max seen_at (t2)
    engine = await databases.project(PROJECT_ID)
    async with engine.connect() as conn:
        cursor_val = (
            await conn.execute(
                select(project_settings.c.value).where(project_settings.c.key == CURSOR_KEY)
            )
        ).scalar_one_or_none()
    assert cursor_val == to_iso(t2)


@pytest.mark.anyio
async def test_handoff_mapping_live_to_production_is_honoured(databases: Databases) -> None:
    await databases.project(PROJECT_ID)
    t0 = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)

    observer = PostDeployObserver(
        databases=databases,
        tracking=FakeTracking(),
        handoffs=FakeHandoffs(make_handoff(environments={"live": "production"})),
        promotions=FakePromotions(),  # type: ignore[arg-type]
        waker=RecordingWaker(),
    )
    await observer.synced(PROJECT_ID, now=t0)

    tracking = FakeTracking()
    tracking.deployments.append(
        DeploymentRow(
            source_id="src_1",
            model="fraud_detection",
            alias="live",
            from_version="v1",
            to_version="v2",
            seen_at=t1,
        )
    )

    waker = RecordingWaker()
    observer = PostDeployObserver(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff(environments={"live": "production"})),
        promotions=FakePromotions(),  # type: ignore[arg-type]
        waker=waker,
    )

    await observer.synced(PROJECT_ID, now=t1 + timedelta(seconds=1))

    assert len(waker.wakes) == 1
    assert waker.wakes[0]["key"] == f"fraud_detection:live:v1:v2:{to_iso(t1)}"


@pytest.mark.anyio
async def test_second_synced_with_nothing_new_wakes_no_one(databases: Databases) -> None:
    await databases.project(PROJECT_ID)
    t0 = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)

    observer = PostDeployObserver(
        databases=databases,
        tracking=FakeTracking(),
        handoffs=FakeHandoffs(make_handoff()),
        promotions=FakePromotions(),  # type: ignore[arg-type]
        waker=RecordingWaker(),
    )
    await observer.synced(PROJECT_ID, now=t0)

    tracking = FakeTracking()
    tracking.deployments.append(
        DeploymentRow(
            source_id="src_1",
            model="fraud_detection",
            alias="production",
            from_version="v1",
            to_version="v2",
            seen_at=t1,
        )
    )

    waker = RecordingWaker()
    observer = PostDeployObserver(
        databases=databases,
        tracking=tracking,
        handoffs=FakeHandoffs(make_handoff()),
        promotions=FakePromotions(),  # type: ignore[arg-type]
        waker=waker,
    )

    # First sync wakes once
    await observer.synced(PROJECT_ID, now=t1 + timedelta(seconds=1))
    assert len(waker.wakes) == 1

    # Second sync with same tracking data wakes no one
    await observer.synced(PROJECT_ID, now=t1 + timedelta(minutes=5))
    assert len(waker.wakes) == 1


@pytest.mark.anyio
async def test_services_wires_post_deploy_observer(tmp_path: Path) -> None:
    async with started_app(tmp_path / "data") as app:
        services: Services = app.state.services
        assert any(isinstance(o, PostDeployObserver) for o in services.scheduler.sync_observers)
