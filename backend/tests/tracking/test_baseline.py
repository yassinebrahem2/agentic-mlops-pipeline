"""Tests for tracking baseline deployments (#649, Task 1 of Plan #664)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select

from mlops_agents.monitoring.incidents.contract import AgentWaker, WakeLabel
from mlops_agents.promotion.contract import Promotions, PromotionView
from mlops_agents.promotion.post_deploy import PostDeployObserver
from mlops_agents.proposals.contract import HandoffPayload, Handoffs, HandoffView
from mlops_agents.storage.tables import deployments, tracking_sources
from mlops_agents.tracking.contract import (
    DeploymentRow,
    ModelVersionRow,
    SyncResult,
    TrackingQuery,
)
from tests.proposals.samples import handoff as sample_handoff
from tests.proposals.samples import model as sample_model
from tests.tracking.conftest import TrackingEnv


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


class FakePromotions(Promotions):
    def __init__(self, history_list: list[PromotionView] | None = None) -> None:
        self._history = history_list or []

    async def validate(self, project_id: str, payload: Any) -> list[str]:
        return []

    async def history(self, project_id: str, model: str | None = None) -> list[PromotionView]:
        if model:
            return [p for p in self._history if p.model == model]
        return list(self._history)


class FakeHandoffs(Handoffs):
    def __init__(self, current_view: HandoffView | None = None) -> None:
        self.current_handoff = current_view

    async def current(self, project_id: str) -> HandoffView | None:
        return self.current_handoff

    async def store(self, project_id: str, proposal_id: str, payload: HandoffPayload) -> int:
        return 1

    def add_observer(self, observer: Any) -> None:
        pass


def make_handoff(models: list[tuple[str, str]] | None = None) -> HandoffView:
    if models is None:
        models = [("classifier", "production")]
    raw = sample_handoff(
        models=[
            sample_model(
                m_name,
                environments={prod_alias: "production", "staging": "staging"},
                production_signal={"kind": "tracker_alias", "ref": prod_alias},
            )
            for m_name, prod_alias in models
        ]
    )
    payload = HandoffPayload.model_validate(raw)
    return HandoffView(
        proposal_id="prp_test",
        version=1,
        approved=datetime.now(UTC),
        payload=payload,
    )


@pytest.mark.anyio
async def test_first_sync_sets_baseline_true_and_subsequent_sets_baseline_false(
    tracking_env: TrackingEnv,
) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )

    t0 = datetime(2026, 10, 28, 10, 0, 0, tzinfo=UTC)
    env.clock.set(t0)

    # First sync has champion at v1
    env.adapters["mlflow"].result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=source.id,
                model="classifier",
                version="1",
                run_key="run_1",
                aliases=[],
                created=t0,
                evaluation="present",
            )
        ],
        runs=[],
        metrics=[],
        aliases={"classifier": {"production": "1"}},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    # Verify baseline in database and query
    engine = await env.databases.project(env.project_id)
    async with engine.connect() as conn:
        db_rows = list(
            await conn.execute(
                select(
                    deployments.c.model,
                    deployments.c.alias,
                    deployments.c.from_version,
                    deployments.c.to_version,
                    deployments.c.baseline,
                )
            )
        )
        assert len(db_rows) == 1
        assert db_rows[0] == ("classifier", "production", None, "1", 1)

    page = await env.service.query(env.project_id, TrackingQuery(what="deployments"))
    assert len(page.rows) == 1
    dep_0 = page.rows[0]
    assert isinstance(dep_0, DeploymentRow)
    assert dep_0.baseline is True

    # Second sync: move 1 -> 2
    t1 = t0 + timedelta(minutes=5)
    env.clock.set(t1)
    env.adapters["mlflow"].result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=source.id,
                model="classifier",
                version="2",
                run_key="run_2",
                aliases=[],
                created=t1,
                evaluation="present",
            )
        ],
        runs=[],
        metrics=[],
        aliases={"classifier": {"production": "2"}},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    async with engine.connect() as conn:
        db_rows = list(
            await conn.execute(
                select(
                    deployments.c.model,
                    deployments.c.to_version,
                    deployments.c.baseline,
                ).order_by(deployments.c.seen_at.asc())
            )
        )
        assert len(db_rows) == 2
        assert db_rows[0] == ("classifier", "1", 1)
        assert db_rows[1] == ("classifier", "2", 0)

    page2 = await env.service.query(env.project_id, TrackingQuery(what="deployments"))
    assert len(page2.rows) == 2
    # newest first
    assert page2.rows[0].to_version == "2"
    assert page2.rows[0].baseline is False
    assert page2.rows[1].to_version == "1"
    assert page2.rows[1].baseline is True


@pytest.mark.anyio
async def test_649_reproduction_observer_first_sync_champion_then_move_wakes_only_move(
    tracking_env: TrackingEnv,
) -> None:
    """The order from #649: observer's first run, then source's first sync recording the champion,
    then a move 1 -> 2: gives exactly one post_deploy wake, for the move."""
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )

    handoffs = FakeHandoffs(make_handoff([("classifier", "production")]))
    promotions = FakePromotions()
    waker = RecordingWaker()

    observer = PostDeployObserver(
        databases=env.databases,
        tracking=env.service,
        handoffs=handoffs,
        promotions=promotions,
        waker=waker,
    )

    t0 = datetime(2026, 10, 28, 10, 0, 0, tzinfo=UTC)
    env.clock.set(t0)

    # 1. Observer's first run: seeds cursor to t0
    await observer.synced(env.project_id, now=t0)
    assert len(waker.wakes) == 0

    # 2. Source's first sync at t1 > t0: records champion (from_version=None, to_version=1)
    t1 = t0 + timedelta(minutes=1)
    env.clock.set(t1)
    env.adapters["mlflow"].result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=source.id,
                model="classifier",
                version="1",
                run_key="run_1",
                aliases=[],
                created=t1,
                evaluation="present",
            )
        ],
        runs=[],
        metrics=[],
        aliases={"classifier": {"production": "1"}},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    # Observer runs after first sync: MUST NOT wake because row is baseline!
    await observer.synced(env.project_id, now=t1)
    assert len(waker.wakes) == 0

    # 3. Source's second sync at t2: move 1 -> 2
    t2 = t1 + timedelta(minutes=1)
    env.clock.set(t2)
    env.adapters["mlflow"].result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=source.id,
                model="classifier",
                version="2",
                run_key="run_2",
                aliases=[],
                created=t2,
                evaluation="present",
            )
        ],
        runs=[],
        metrics=[],
        aliases={"classifier": {"production": "2"}},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    # Observer runs after second sync: MUST wake once for move 1 -> 2!
    await observer.synced(env.project_id, now=t2)
    assert len(waker.wakes) == 1
    assert '<data source="tracking">\n1\n</data>' in waker.wakes[0]["text"]
    assert '<data source="tracking">\n2\n</data>' in waker.wakes[0]["text"]


@pytest.mark.anyio
async def test_new_model_first_production_alias_after_first_sync_wakes_once(
    tracking_env: TrackingEnv,
) -> None:
    """A new model whose first production alias appears after the source's first sync wakes once
    (proves skipping from_version=None is NOT used; real first deploys wake)."""
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )

    handoffs = FakeHandoffs(
        make_handoff([("classifier", "production"), ("fraud_detector", "production")])
    )
    promotions = FakePromotions()
    waker = RecordingWaker()

    observer = PostDeployObserver(
        databases=env.databases,
        tracking=env.service,
        handoffs=handoffs,
        promotions=promotions,
        waker=waker,
    )

    t0 = datetime(2026, 10, 28, 10, 0, 0, tzinfo=UTC)
    env.clock.set(t0)
    await observer.synced(env.project_id, now=t0)

    # First sync has classifier champion
    t1 = t0 + timedelta(minutes=1)
    env.clock.set(t1)
    env.adapters["mlflow"].result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=source.id,
                model="classifier",
                version="1",
                run_key="run_1",
                aliases=[],
                created=t1,
                evaluation="present",
            )
        ],
        runs=[],
        metrics=[],
        aliases={"classifier": {"production": "1"}},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)
    await observer.synced(env.project_id, now=t1)
    assert len(waker.wakes) == 0

    # Second sync: A NEW model 'fraud_detector' gets its very first production alias (from_version=None, to_version=v1)
    t2 = t1 + timedelta(minutes=1)
    env.clock.set(t2)
    env.adapters["mlflow"].result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=source.id,
                model="fraud_detector",
                version="v1",
                run_key="run_fd1",
                aliases=[],
                created=t2,
                evaluation="present",
            )
        ],
        runs=[],
        metrics=[],
        aliases={
            "classifier": {"production": "1"},
            "fraud_detector": {"production": "v1"},
        },
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    # Observer must wake once for fraud_detector even though from_version is None
    await observer.synced(env.project_id, now=t2)
    assert len(waker.wakes) == 1
    wake = waker.wakes[0]
    assert "fraud_detector" in wake["text"]
    assert "Old version: none" in wake["text"]
    assert '<data source="tracking">\nv1\n</data>' in wake["text"]


@pytest.mark.anyio
async def test_source_first_sync_saw_no_alias_then_first_alias_wakes_once(
    tracking_env: TrackingEnv,
) -> None:
    """A source whose first sync saw no alias, then a first alias, wakes once."""
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )

    handoffs = FakeHandoffs(make_handoff([("classifier", "production")]))
    promotions = FakePromotions()
    waker = RecordingWaker()

    observer = PostDeployObserver(
        databases=env.databases,
        tracking=env.service,
        handoffs=handoffs,
        promotions=promotions,
        waker=waker,
    )

    t0 = datetime(2026, 10, 28, 10, 0, 0, tzinfo=UTC)
    env.clock.set(t0)
    await observer.synced(env.project_id, now=t0)

    # First sync saw no aliases at all
    t1 = t0 + timedelta(minutes=1)
    env.clock.set(t1)
    env.adapters["mlflow"].result = SyncResult(
        versions=[],
        runs=[],
        metrics=[],
        aliases={},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)
    await observer.synced(env.project_id, now=t1)
    assert len(waker.wakes) == 0

    # Second sync: first alias appears!
    t2 = t1 + timedelta(minutes=1)
    env.clock.set(t2)
    env.adapters["mlflow"].result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=source.id,
                model="classifier",
                version="1",
                run_key="run_1",
                aliases=[],
                created=t2,
                evaluation="present",
            )
        ],
        runs=[],
        metrics=[],
        aliases={"classifier": {"production": "1"}},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    await observer.synced(env.project_id, now=t2)
    assert len(waker.wakes) == 1
    assert "classifier" in waker.wakes[0]["text"]
    assert '<data source="tracking">\n1\n</data>' in waker.wakes[0]["text"]


@pytest.mark.anyio
async def test_restart_rewakes_nothing(
    tracking_env: TrackingEnv,
) -> None:
    """A restart re-wakes nothing."""
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )

    handoffs = FakeHandoffs(make_handoff([("classifier", "production")]))
    promotions = FakePromotions()
    waker = RecordingWaker()

    observer = PostDeployObserver(
        databases=env.databases,
        tracking=env.service,
        handoffs=handoffs,
        promotions=promotions,
        waker=waker,
    )

    t0 = datetime(2026, 10, 28, 10, 0, 0, tzinfo=UTC)
    env.clock.set(t0)
    await observer.synced(env.project_id, now=t0)

    # First sync: baseline
    t1 = t0 + timedelta(minutes=1)
    env.clock.set(t1)
    env.adapters["mlflow"].result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=source.id,
                model="classifier",
                version="1",
                run_key="run_1",
                aliases=[],
                created=t1,
                evaluation="present",
            )
        ],
        runs=[],
        metrics=[],
        aliases={"classifier": {"production": "1"}},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)
    await observer.synced(env.project_id, now=t1)

    # Second sync: move 1 -> 2 wakes once
    t2 = t1 + timedelta(minutes=1)
    env.clock.set(t2)
    env.adapters["mlflow"].result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=source.id,
                model="classifier",
                version="2",
                run_key="run_2",
                aliases=[],
                created=t2,
                evaluation="present",
            )
        ],
        runs=[],
        metrics=[],
        aliases={"classifier": {"production": "2"}},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)
    await observer.synced(env.project_id, now=t2)
    assert len(waker.wakes) == 1

    # Restart: instantiate a new observer with fresh waker
    waker2 = RecordingWaker()
    observer_restarted = PostDeployObserver(
        databases=env.databases,
        tracking=env.service,
        handoffs=handoffs,
        promotions=promotions,
        waker=waker2,
    )
    await observer_restarted.synced(env.project_id, now=t2 + timedelta(minutes=1))
    assert len(waker2.wakes) == 0


@pytest.mark.anyio
async def test_removed_source_readded_starts_new_baseline(
    tracking_env: TrackingEnv,
) -> None:
    """A source removed and added again starts a new baseline (its first sync again)."""
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )

    t0 = datetime(2026, 10, 28, 10, 0, 0, tzinfo=UTC)
    env.clock.set(t0)
    env.adapters["mlflow"].result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=source.id,
                model="classifier",
                version="1",
                run_key="run_1",
                aliases=[],
                created=t0,
                evaluation="present",
            )
        ],
        runs=[],
        metrics=[],
        aliases={"classifier": {"production": "1"}},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    # Delete source and create a new source
    engine = await env.databases.project(env.project_id)
    async with engine.begin() as conn:
        await conn.execute(delete(tracking_sources).where(tracking_sources.c.id == source.id))

    t1 = t0 + timedelta(minutes=5)
    env.clock.set(t1)
    source2 = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )
    env.adapters["mlflow"].result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=source2.id,
                model="classifier",
                version="2",
                run_key="run_2",
                aliases=[],
                created=t1,
                evaluation="present",
            )
        ],
        runs=[],
        metrics=[],
        aliases={"classifier": {"production": "2"}},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source2.id)

    # The new source's first sync must also be baseline
    page = await env.service.query(
        env.project_id, TrackingQuery(what="deployments", source_id=source2.id)
    )
    assert len(page.rows) == 1
    assert page.rows[0].baseline is True
