"""Tracking store: atomic write of sync results and querying records."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from mlops_agents.clock import to_iso
from mlops_agents.storage.tables import (
    ci_runs,
    deployments,
    evaluations,
    model_versions,
    run_metrics,
    tracked_models,
    tracked_runs,
    tracking_sources,
)
from mlops_agents.tracking.contract import SyncResult
from mlops_agents.tracking.queries import (
    query_ci_runs,
    query_deployments,
    query_evaluations,
    query_metrics,
    query_models,
    query_runs,
    query_versions,
)

__all__ = [
    "query_ci_runs",
    "query_deployments",
    "query_evaluations",
    "query_metrics",
    "query_models",
    "query_runs",
    "query_versions",
    "save_sync_result",
]


async def _save_models(conn: AsyncConnection, source_id: str, result: SyncResult) -> None:
    models_to_track: set[str] = set()
    for v in result.versions:
        models_to_track.add(v.model)
    for m in result.aliases:
        models_to_track.add(m)
    for e in result.evaluations:
        models_to_track.add(e.model)

    for model_name in models_to_track:
        stmt = sqlite_insert(tracked_models).values(source_id=source_id, name=model_name)
        stmt = stmt.on_conflict_do_nothing()
        await conn.execute(stmt)


async def _save_deployments(
    conn: AsyncConnection,
    source_id: str,
    result: SyncResult,
    now_iso: str,
) -> None:
    dep_rows = (
        await conn.execute(
            select(
                deployments.c.model,
                deployments.c.alias,
                deployments.c.to_version,
                deployments.c.seen_at,
            )
            .where(deployments.c.source_id == source_id)
            .order_by(deployments.c.seen_at.asc())
        )
    ).all()

    current_state: dict[tuple[str, str], str | None] = {}
    last_seen_at: dict[tuple[str, str], str] = {}
    for r in dep_rows:
        current_state[(r.model, r.alias)] = r.to_version
        last_seen_at[(r.model, r.alias)] = r.seen_at

    def _next_seen_at(model: str, alias: str, base_iso: str) -> str:
        prev = last_seen_at.get((model, alias))
        if prev is not None and prev >= base_iso:
            prev_dt = datetime.fromisoformat(prev)
            new_dt = prev_dt + timedelta(milliseconds=1)
            next_iso = to_iso(new_dt)
            last_seen_at[(model, alias)] = next_iso
            return next_iso
        last_seen_at[(model, alias)] = base_iso
        return base_iso

    # For each model -> alias -> version in result.aliases
    for model, alias_map in result.aliases.items():
        for alias, target_version in alias_map.items():
            current_ver = current_state.get((model, alias))
            if current_ver != target_version:
                seen_at_str = _next_seen_at(model, alias, now_iso)
                stmt_dep = sqlite_insert(deployments).values(
                    source_id=source_id,
                    model=model,
                    alias=alias,
                    seen_at=seen_at_str,
                    from_version=current_ver,
                    to_version=target_version,
                )
                await conn.execute(stmt_dep)
                current_state[(model, alias)] = target_version

    # An alias in current state and absent from result.aliases
    for (model, alias), current_ver in list(current_state.items()):
        if current_ver is not None and (
            model not in result.aliases or alias not in result.aliases[model]
        ):
            seen_at_str = _next_seen_at(model, alias, now_iso)
            stmt_dep = sqlite_insert(deployments).values(
                source_id=source_id,
                model=model,
                alias=alias,
                seen_at=seen_at_str,
                from_version=current_ver,
                to_version=None,
            )
            await conn.execute(stmt_dep)
            current_state[(model, alias)] = None


def _derive_version_aliases(
    result: SyncResult,
) -> dict[str, dict[str, list[str]]]:
    version_aliases_by_model: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for model, alias_map in result.aliases.items():
        for alias, ver in alias_map.items():
            version_aliases_by_model[model][ver].append(alias)

    for m in version_aliases_by_model:
        for v in version_aliases_by_model[m]:
            version_aliases_by_model[m][v].sort()

    return version_aliases_by_model


async def _save_aliases(
    conn: AsyncConnection,
    source_id: str,
    version_aliases_by_model: dict[str, dict[str, list[str]]],
) -> None:
    existing_mv_rows = (
        await conn.execute(
            select(model_versions.c.model, model_versions.c.version).where(
                model_versions.c.source_id == source_id
            )
        )
    ).all()
    for row in existing_mv_rows:
        new_aliases = version_aliases_by_model[row.model].get(row.version, [])
        await conn.execute(
            update(model_versions)
            .where(
                model_versions.c.source_id == source_id,
                model_versions.c.model == row.model,
                model_versions.c.version == row.version,
            )
            .values(aliases=json.dumps(new_aliases))
        )


async def _save_versions(
    conn: AsyncConnection,
    source_id: str,
    result: SyncResult,
    version_aliases_by_model: dict[str, dict[str, list[str]]],
) -> None:
    for v in result.versions:
        derived_aliases = version_aliases_by_model[v.model].get(v.version, [])
        mv_stmt = sqlite_insert(model_versions).values(
            source_id=source_id,
            model=v.model,
            version=v.version,
            run_key=v.run_key,
            aliases=json.dumps(derived_aliases),
            created_at=to_iso(v.created),
            evaluation=v.evaluation,
        )
        mv_stmt = mv_stmt.on_conflict_do_update(
            index_elements=[
                model_versions.c.source_id,
                model_versions.c.model,
                model_versions.c.version,
            ],
            set_={
                "run_key": mv_stmt.excluded.run_key,
                "aliases": mv_stmt.excluded.aliases,
                "evaluation": mv_stmt.excluded.evaluation,
            },
        )
        await conn.execute(mv_stmt)


async def _save_runs(conn: AsyncConnection, source_id: str, result: SyncResult) -> None:
    for r in result.runs:
        r_stmt = sqlite_insert(tracked_runs).values(
            source_id=source_id,
            key=r.key,
            experiment=r.experiment,
            status=r.status,
            started_at=to_iso(r.start) if r.start else None,
            ended_at=to_iso(r.end) if r.end else None,
            params=json.dumps(r.params),
            tags=json.dumps(r.tags),
            git_commit=r.git_commit,
            datasets=json.dumps(r.datasets),
        )
        r_stmt = r_stmt.on_conflict_do_update(
            index_elements=[tracked_runs.c.source_id, tracked_runs.c.key],
            set_={
                "experiment": r_stmt.excluded.experiment,
                "status": r_stmt.excluded.status,
                "started_at": r_stmt.excluded.started_at,
                "ended_at": r_stmt.excluded.ended_at,
                "params": r_stmt.excluded.params,
                "tags": r_stmt.excluded.tags,
                "git_commit": r_stmt.excluded.git_commit,
                "datasets": r_stmt.excluded.datasets,
            },
        )
        await conn.execute(r_stmt)


async def _save_metrics(conn: AsyncConnection, source_id: str, result: SyncResult) -> None:
    for m in result.metrics:
        m_stmt = sqlite_insert(run_metrics).values(
            source_id=source_id,
            run_key=m.run_key,
            metric=m.metric,
            step=m.step,
            time=to_iso(m.time),
            value=m.value,
        )
        m_stmt = m_stmt.on_conflict_do_update(
            index_elements=[
                run_metrics.c.source_id,
                run_metrics.c.run_key,
                run_metrics.c.metric,
                run_metrics.c.step,
                run_metrics.c.time,
            ],
            set_={"value": m_stmt.excluded.value},
        )
        await conn.execute(m_stmt)


async def _save_ci_runs(conn: AsyncConnection, source_id: str, result: SyncResult) -> None:
    for c in result.ci_runs:
        c_stmt = sqlite_insert(ci_runs).values(
            source_id=source_id,
            key=c.key,
            workflow=c.workflow,
            status=c.status,
            started_at=to_iso(c.start),
            duration_s=c.duration_s,
            commit_sha=c.commit,
            url=c.url,
        )
        c_stmt = c_stmt.on_conflict_do_update(
            index_elements=[ci_runs.c.source_id, ci_runs.c.key],
            set_={
                "workflow": c_stmt.excluded.workflow,
                "status": c_stmt.excluded.status,
                "started_at": c_stmt.excluded.started_at,
                "duration_s": c_stmt.excluded.duration_s,
                "commit_sha": c_stmt.excluded.commit_sha,
                "url": c_stmt.excluded.url,
            },
        )
        await conn.execute(c_stmt)


async def _save_evaluations(conn: AsyncConnection, source_id: str, result: SyncResult) -> None:
    for e in result.evaluations:
        e_stmt = sqlite_insert(evaluations).values(
            source_id=source_id,
            model=e.model,
            version=e.version,
            run_key=e.run_key,
            schema_version=e.schema_version,
            payload=json.dumps(e.payload),
            fetched_at=to_iso(e.fetched_at),
        )
        e_stmt = e_stmt.on_conflict_do_update(
            index_elements=[evaluations.c.source_id, evaluations.c.model, evaluations.c.version],
            set_={
                "run_key": e_stmt.excluded.run_key,
                "schema_version": e_stmt.excluded.schema_version,
                "payload": e_stmt.excluded.payload,
                "fetched_at": e_stmt.excluded.fetched_at,
            },
        )
        await conn.execute(e_stmt)


async def save_sync_result(
    conn: AsyncConnection,
    source_id: str,
    result: SyncResult,
    clock_now: datetime,
) -> None:
    now_iso = to_iso(clock_now)

    await _save_models(conn, source_id, result)
    await _save_deployments(conn, source_id, result, now_iso)
    aliases_map = _derive_version_aliases(result)
    await _save_aliases(conn, source_id, aliases_map)
    await _save_versions(conn, source_id, result, aliases_map)
    await _save_runs(conn, source_id, result)
    await _save_metrics(conn, source_id, result)
    await _save_ci_runs(conn, source_id, result)
    await _save_evaluations(conn, source_id, result)

    await conn.execute(
        update(tracking_sources)
        .where(tracking_sources.c.id == source_id)
        .values(last_sync=now_iso, last_error=None)
    )
