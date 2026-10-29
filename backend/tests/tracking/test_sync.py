"""Tests for tracking sync transactions, row updates, deployments, and concurrency."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select

from mlops_agents.storage.tables import (
    ci_runs,
    deployments,
    evaluations,
    model_versions,
    run_metrics,
    tracked_models,
    tracked_runs,
)
from mlops_agents.tracking.contract import (
    CiRunRow,
    EvaluationRecord,
    MetricPoint,
    ModelVersionRow,
    RunRow,
    SyncResult,
)
from tests.tracking.conftest import TrackingEnv


async def test_first_sync_and_identical_sync(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )

    t0 = datetime(2026, 10, 28, 10, 0, 0, tzinfo=UTC)
    result = SyncResult(
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
        runs=[
            RunRow(
                source_id=source.id,
                key="run_1",
                experiment="exp_default",
                status="running",
                start=t0,
                end=None,
                params={"lr": "0.01"},
                tags={"framework": "pytorch"},
                git_commit="abc1234",
                datasets=["mnist"],
            )
        ],
        metrics=[
            MetricPoint(
                source_id=source.id,
                run_key="run_1",
                metric="accuracy",
                step=1,
                value=0.95,
                time=t0,
            )
        ],
        aliases={"classifier": {"production": "1"}},
        ci_runs=[
            CiRunRow(
                source_id=source.id,
                key="ci_1",
                workflow="test.yml",
                status="succeeded",
                start=t0,
                duration_s=45.2,
                commit="abc1234",
                url="https://github.com/runs/1",
            )
        ],
        evaluations=[
            EvaluationRecord(
                source_id=source.id,
                model="classifier",
                version="1",
                run_key="run_1",
                schema_version=1,
                payload={"f1": 0.94},
                fetched_at=t0,
            )
        ],
        error=None,
    )
    env.adapters["mlflow"].result = result

    # Run first sync
    await env.service.run_sync(env.project_id, source.id)

    # Verify rows written
    engine = await env.databases.project(env.project_id)
    async with engine.connect() as conn:
        assert (await conn.scalar(select(func.count(tracked_models.c.name)))) == 1
        assert (await conn.scalar(select(func.count(model_versions.c.version)))) == 1
        assert (await conn.scalar(select(func.count(tracked_runs.c.key)))) == 1
        assert (await conn.scalar(select(func.count(run_metrics.c.step)))) == 1
        assert (await conn.scalar(select(func.count(ci_runs.c.key)))) == 1
        assert (await conn.scalar(select(func.count(evaluations.c.version)))) == 1

        # Check deployment created with from_version=None
        deps = list(
            await conn.execute(
                select(
                    deployments.c.model,
                    deployments.c.alias,
                    deployments.c.from_version,
                    deployments.c.to_version,
                )
            )
        )
        assert len(deps) == 1
        assert deps[0] == ("classifier", "production", None, "1")

        # Check model_versions aliases derived by service
        mv_aliases = await conn.scalar(select(model_versions.c.aliases))
        assert mv_aliases == '["production"]'

    # Check tracking.synced event
    evts = await env.events.history(env.project_id, types=["tracking.synced"])
    assert len(evts) == 1
    p = evts[0].payload
    assert p["ok"] is True
    assert p["versions"] == 1
    assert p["runs"] == 1
    assert p["metrics"] == 1
    assert p["ci_runs"] == 1
    assert p["evaluations"] == 1
    assert p["error"] is None

    # Run second identical sync: must add NO rows to any table
    await env.service.run_sync(env.project_id, source.id)
    async with engine.connect() as conn:
        assert (await conn.scalar(select(func.count(tracked_models.c.name)))) == 1
        assert (await conn.scalar(select(func.count(model_versions.c.version)))) == 1
        assert (await conn.scalar(select(func.count(tracked_runs.c.key)))) == 1
        assert (await conn.scalar(select(func.count(run_metrics.c.step)))) == 1
        assert (await conn.scalar(select(func.count(ci_runs.c.key)))) == 1
        assert (await conn.scalar(select(func.count(evaluations.c.version)))) == 1
        assert (await conn.scalar(select(func.count(deployments.c.alias)))) == 1


async def test_alias_change_and_removal(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )
    t0 = datetime(2026, 10, 28, 10, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 10, 28, 11, 0, 0, tzinfo=UTC)

    # Initial sync: production -> v1
    env.adapters["mlflow"].result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=source.id,
                model="classifier",
                version="1",
                run_key="run_1",
                aliases=[],
                created=t0,
                evaluation="absent",
            ),
            ModelVersionRow(
                source_id=source.id,
                model="classifier",
                version="2",
                run_key="run_2",
                aliases=[],
                created=t1,
                evaluation="absent",
            ),
        ],
        runs=[],
        metrics=[],
        aliases={"classifier": {"production": "1"}},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    # Second sync: production alias changed to v2
    env.clock.advance(60)
    env.adapters["mlflow"].result.aliases = {"classifier": {"production": "2"}}
    await env.service.run_sync(env.project_id, source.id)

    engine = await env.databases.project(env.project_id)
    async with engine.connect() as conn:
        deps = list(
            await conn.execute(
                select(deployments.c.from_version, deployments.c.to_version).order_by(
                    deployments.c.seen_at.asc()
                )
            )
        )
        assert len(deps) == 2
        assert deps[0] == (None, "1")
        assert deps[1] == ("1", "2")

    # Third sync: production alias removed
    env.clock.advance(60)
    env.adapters["mlflow"].result.aliases = {}
    await env.service.run_sync(env.project_id, source.id)

    async with engine.connect() as conn:
        deps = list(
            await conn.execute(
                select(deployments.c.from_version, deployments.c.to_version).order_by(
                    deployments.c.seen_at.asc()
                )
            )
        )
        assert len(deps) == 3
        assert deps[2] == ("2", None)

    # Fourth sync: alias still absent -> adds nothing!
    env.clock.advance(60)
    await env.service.run_sync(env.project_id, source.id)
    async with engine.connect() as conn:
        count = await conn.scalar(select(func.count(deployments.c.alias)))
        assert count == 3


async def test_run_status_update_and_evaluation_update(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )
    t0 = datetime(2026, 10, 28, 10, 0, 0, tzinfo=UTC)

    env.adapters["mlflow"].result = SyncResult(
        versions=[],
        runs=[
            RunRow(
                source_id=source.id,
                key="run_1",
                experiment="exp",
                status="running",
                start=t0,
                end=None,
                params={},
                tags={},
                git_commit=None,
                datasets=[],
            )
        ],
        metrics=[],
        aliases={},
        ci_runs=[],
        evaluations=[
            EvaluationRecord(
                source_id=source.id,
                model="m1",
                version="1",
                run_key="run_1",
                schema_version=1,
                payload={"acc": 0.8},
                fetched_at=t0,
            )
        ],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    # Next sync: run finished (succeeded), evaluation payload updated
    env.adapters["mlflow"].result.runs[0].status = "succeeded"
    env.adapters["mlflow"].result.evaluations[0].payload = {"acc": 0.9}
    await env.service.run_sync(env.project_id, source.id)

    engine = await env.databases.project(env.project_id)
    async with engine.connect() as conn:
        run_row = (await conn.execute(select(tracked_runs.c.status))).one()
        assert run_row[0] == "succeeded"

        eval_row = (await conn.execute(select(evaluations.c.payload))).one()
        assert eval_row[0] == '{"acc": 0.9}'


async def test_adapter_failure_preserves_rows_and_good_sync_clears_error(
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

    # Good sync
    env.adapters["mlflow"].result = SyncResult(
        versions=[
            ModelVersionRow(
                source_id=source.id,
                model="m",
                version="1",
                run_key=None,
                aliases=[],
                created=t0,
                evaluation="absent",
            )
        ],
        runs=[],
        metrics=[],
        aliases={},
        ci_runs=[],
        evaluations=[],
        error=None,
    )
    await env.service.run_sync(env.project_id, source.id)

    src_view = (await env.service.sources(env.project_id))[0]
    last_sync_good = src_view.last_sync
    assert last_sync_good is not None
    assert src_view.last_error is None

    # Next sync fails
    env.adapters["mlflow"].result = SyncResult(
        versions=[],
        runs=[],
        metrics=[],
        aliases={},
        ci_runs=[],
        evaluations=[],
        error="Remote server crashed",
    )
    await env.service.run_sync(env.project_id, source.id)

    src_failed = (await env.service.sources(env.project_id))[0]
    assert src_failed.last_sync == last_sync_good  # untouched!
    assert src_failed.last_error == "Remote server crashed"

    # Stored rows remain
    engine = await env.databases.project(env.project_id)
    async with engine.connect() as conn:
        count = await conn.scalar(select(func.count(model_versions.c.version)))
        assert count == 1

    # Good sync clears last_error
    env.adapters["mlflow"].result.error = None
    await env.service.run_sync(env.project_id, source.id)

    src_recovered = (await env.service.sources(env.project_id))[0]
    assert src_recovered.last_error is None


async def test_two_sources_one_failing_other_succeeds(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    s1 = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow1.example.com"},
        auth="none",
    )
    s2 = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow2.example.com"},
        auth="none",
    )

    # Custom adapter logic: fail for s1, succeed for s2
    async def selective_sync(source: Any, credential: Any, since: Any) -> SyncResult:
        if source.id == s1.id:
            raise RuntimeError("s1 failure")
        return SyncResult(
            versions=[], runs=[], metrics=[], aliases={}, ci_runs=[], evaluations=[], error=None
        )

    env.adapters["mlflow"].sync = selective_sync  # type: ignore[method-assign]

    # run_sync without source_id runs all sources
    await env.service.run_sync(env.project_id)

    sources = {s.id: s for s in await env.service.sources(env.project_id)}
    assert sources[s1.id].last_error is not None
    assert sources[s2.id].last_error is None
    assert sources[s2.id].last_sync is not None


async def test_concurrent_syncs_absorb(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )
    started = asyncio.Event()
    proceed = asyncio.Event()

    original_sync = env.adapters["mlflow"].sync

    async def controlled_sync(
        source: Any, credential: str | None = None, since: Any = None
    ) -> SyncResult:
        started.set()
        await proceed.wait()
        return await original_sync(source, credential, since=since)

    env.adapters["mlflow"].sync = controlled_sync  # type: ignore[method-assign]

    # Task 1 starts and waits inside controlled_sync, holding the source lock
    t1 = asyncio.create_task(env.service.run_sync(env.project_id, source.id))
    await started.wait()

    # Task 2 starts while task 1 is syncing -> finds lock held and absorbs
    t2 = asyncio.create_task(env.service.run_sync(env.project_id, source.id))
    # Yield control to let task 2 run and observe the held lock
    await asyncio.sleep(0.01)

    # Let task 1 finish
    proceed.set()
    await asyncio.gather(t1, t2)

    # Adapter should have been called only once!
    assert len(env.adapters["mlflow"].calls) == 1


async def test_missing_adapter_records_error(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="github_actions",
        settings={"repository": "owner/repo"},
        auth="none",
    )
    # env only registered mlflow adapter, github_actions has no adapter
    await env.service.run_sync(env.project_id, source.id)

    updated = (await env.service.sources(env.project_id))[0]
    assert updated.last_error == "No adapter for this source type"


async def test_sync_failure_in_store_write_records_error_and_event(
    tracking_env: TrackingEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )

    async def broken_save(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("database disk failure during save")

    monkeypatch.setattr("mlops_agents.tracking.service.save_sync_result", broken_save)

    await env.service.run_sync(env.project_id, source.id)

    updated = (await env.service.sources(env.project_id))[0]
    assert updated.last_error is not None
    assert "database disk failure during save" in updated.last_error

    events = await env.events.history(env.project_id, types=["tracking.synced"])
    assert len(events) == 1
    assert events[0].payload["ok"] is False
    assert "database disk failure during save" in str(events[0].payload["error"])


async def test_github_token_and_credentials_kept_out_of_last_error(
    tracking_env: TrackingEnv,
) -> None:
    env = tracking_env
    env.github_token_value = "ghp_super_secret_github_token_999"

    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="credential",
    )
    await env.service.set_credential(env.project_id, source.id, "my_db_password_xyz")

    async def failing_sync(source: Any, credential: Any, since: Any) -> SyncResult:
        raise RuntimeError(
            "Connection failed with my_db_password_xyz and ghp_super_secret_github_token_999 at http://user:my_db_password_xyz@host/test"
        )

    env.adapters["mlflow"].sync = failing_sync  # type: ignore[method-assign]

    await env.service.run_sync(env.project_id, source.id)

    updated = (await env.service.sources(env.project_id))[0]
    assert updated.last_error is not None
    assert "my_db_password_xyz" not in updated.last_error
    assert "ghp_super_secret_github_token_999" not in updated.last_error


async def test_close_cancels_background_sync(tracking_env: TrackingEnv) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )
    sync_started = asyncio.Event()
    cancelled_seen = asyncio.Event()

    async def hanging_sync(source: Any, credential: Any, since: Any) -> SyncResult:
        sync_started.set()
        try:
            await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            cancelled_seen.set()
            raise
        return SyncResult(
            versions=[], runs=[], metrics=[], aliases={}, ci_runs=[], evaluations=[], error=None
        )

    env.adapters["mlflow"].sync = hanging_sync  # type: ignore[method-assign]

    await env.service.sync(env.project_id, source.id)
    await sync_started.wait()

    await env.service.close()
    assert cancelled_seen.is_set()


async def test_sync_resilient_to_events_record_failure(
    tracking_env: TrackingEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )

    async def fail_record(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Event store write failed")

    monkeypatch.setattr(env.events, "record", fail_record)

    # Both successful sync and error sync should not crash if events.record fails
    env.adapters["mlflow"].result = SyncResult(
        versions=[], runs=[], metrics=[], aliases={}, ci_runs=[], evaluations=[], error=None
    )
    await env.service.run_sync(env.project_id, source.id)
    updated = (await env.service.sources(env.project_id))[0]
    assert updated.last_sync is not None

    env.adapters["mlflow"].result = SyncResult(
        versions=[],
        runs=[],
        metrics=[],
        aliases={},
        ci_runs=[],
        evaluations=[],
        error="Adapter error",
    )
    await env.service.run_sync(env.project_id, source.id)
    updated = (await env.service.sources(env.project_id))[0]
    assert updated.last_error == "Adapter error"


async def test_sync_resilient_to_load_source_row_failure(
    tracking_env: TrackingEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tracking_env
    source = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://mlflow.example.com"},
        auth="none",
    )

    import mlops_agents.tracking.service as svc_mod

    async def fail_load(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Database connection broken during load_source_row")

    monkeypatch.setattr(svc_mod, "load_source_row", fail_load)
    # Should catch exception and not raise
    await env.service.run_sync(env.project_id, source.id)


async def test_sync_all_resilient_to_gather_exception_and_sources_failure(
    tracking_env: TrackingEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tracking_env
    s1 = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://s1.example.com"},
        auth="none",
    )
    s2 = await env.service.create_source(
        project_id=env.project_id,
        type="mlflow",
        settings={"url": "https://s2.example.com"},
        auth="none",
    )

    orig_sync_source = env.service._sync_source  # pyright: ignore[reportPrivateUsage]

    async def fail_one_source(project_id: str, source_id: str) -> None:
        if source_id == s1.id:
            raise RuntimeError("Unexpected gather crash on source 1")
        await orig_sync_source(project_id, source_id)

    monkeypatch.setattr(env.service, "_sync_source", fail_one_source)

    # Sync all sources: source 1 raises unexpected exception, source 2 completes cleanly
    env.adapters["mlflow"].result = SyncResult(
        versions=[], runs=[], metrics=[], aliases={}, ci_runs=[], evaluations=[], error=None
    )
    await env.service.run_sync(env.project_id)

    srcs = {s.id: s for s in await env.service.sources(env.project_id)}
    assert srcs[s2.id].last_sync is not None

    # Now test when self.sources() fails during sync-all
    async def fail_sources(project_id: str) -> Any:
        raise RuntimeError("sources query failed")

    monkeypatch.setattr(env.service, "sources", fail_sources)
    await env.service.run_sync(env.project_id)
