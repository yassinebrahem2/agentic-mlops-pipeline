"""Tracking store: atomic write of sync results and querying records."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from mlops_agents.errors import DomainError
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
from mlops_agents.tracking.constants import PRODUCTION_ALIAS
from mlops_agents.tracking.contract import (
    CiRunRow,
    DeploymentRow,
    EvaluationRecord,
    MetricPoint,
    ModelSummary,
    ModelVersionRow,
    Page,
    RunRow,
    SyncResult,
    TrackingQuery,
)


def _parse_dt(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    return datetime.fromisoformat(raw)


def _require_dt(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


async def save_sync_result(
    conn: AsyncConnection,
    source_id: str,
    result: SyncResult,
    clock_now: datetime,
) -> None:
    now_iso = clock_now.isoformat()

    # 1. Ensure tracked_models exists for all model names
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

    # 2. Deployments derivation
    # Read current state of aliases from deployments table
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
            next_iso = new_dt.isoformat()
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

    # 3. ModelVersionRow.aliases owned by the service
    # Invert result.aliases map per model: version -> list[alias]
    version_aliases_by_model: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for model, alias_map in result.aliases.items():
        for alias, ver in alias_map.items():
            version_aliases_by_model[model][ver].append(alias)

    # Sort aliases for stability
    for m in version_aliases_by_model:
        for v in version_aliases_by_model[m]:
            version_aliases_by_model[m][v].sort()

    # Overwrite stored versions' aliases for all models of this source
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

    # 4. Upsert model_versions
    for v in result.versions:
        derived_aliases = version_aliases_by_model[v.model].get(v.version, [])
        mv_stmt = sqlite_insert(model_versions).values(
            source_id=source_id,
            model=v.model,
            version=v.version,
            run_key=v.run_key,
            aliases=json.dumps(derived_aliases),
            created_at=v.created.isoformat(),
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

    # 5. Upsert tracked_runs
    for r in result.runs:
        r_stmt = sqlite_insert(tracked_runs).values(
            source_id=source_id,
            key=r.key,
            experiment=r.experiment,
            status=r.status,
            started_at=r.start.isoformat() if r.start else None,
            ended_at=r.end.isoformat() if r.end else None,
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

    # 6. Upsert run_metrics
    for m in result.metrics:
        m_stmt = sqlite_insert(run_metrics).values(
            source_id=source_id,
            run_key=m.run_key,
            metric=m.metric,
            step=m.step,
            time=m.time.isoformat(),
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

    # 7. Upsert ci_runs
    for c in result.ci_runs:
        c_stmt = sqlite_insert(ci_runs).values(
            source_id=source_id,
            key=c.key,
            workflow=c.workflow,
            status=c.status,
            started_at=c.start.isoformat(),
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

    # 8. Upsert evaluations
    for e in result.evaluations:
        e_stmt = sqlite_insert(evaluations).values(
            source_id=source_id,
            model=e.model,
            version=e.version,
            run_key=e.run_key,
            schema_version=e.schema_version,
            payload=json.dumps(e.payload),
            fetched_at=e.fetched_at.isoformat(),
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

    # 9. Update tracking_sources last_sync and clear last_error
    await conn.execute(
        update(tracking_sources)
        .where(tracking_sources.c.id == source_id)
        .values(last_sync=now_iso, last_error=None)
    )


async def query_models(conn: AsyncConnection, q: TrackingQuery) -> Page[ModelSummary]:
    stmt = select(tracked_models.c.source_id, tracked_models.c.name)
    if q.source_id:
        stmt = stmt.where(tracked_models.c.source_id == q.source_id)
    if q.model:
        stmt = stmt.where(tracked_models.c.name == q.model)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total: int = int((await conn.scalar(count_stmt)) or 0)

    stmt = stmt.order_by(tracked_models.c.name.asc(), tracked_models.c.source_id.asc())
    stmt = stmt.limit(q.limit).offset(q.offset)

    model_rows = (await conn.execute(stmt)).all()
    summaries: list[ModelSummary] = []

    for src_id, name in model_rows:
        # Query versions of this model
        v_rows = (
            await conn.execute(
                select(
                    model_versions.c.version,
                    model_versions.c.aliases,
                    model_versions.c.created_at,
                )
                .where(
                    model_versions.c.source_id == src_id,
                    model_versions.c.model == name,
                )
                .order_by(model_versions.c.created_at.desc(), model_versions.c.version.desc())
            )
        ).all()

        versions_count = len(v_rows)
        latest_version = v_rows[0].version if v_rows else None
        prod_version: str | None = None
        for vr in v_rows:
            aliases_list = json.loads(vr.aliases)
            if PRODUCTION_ALIAS in aliases_list:
                prod_version = vr.version
                break

        summaries.append(
            ModelSummary(
                source_id=src_id,
                name=name,
                production_version=prod_version,
                latest_version=latest_version,
                versions=versions_count,
            )
        )

    truncated = total > q.offset + len(summaries)
    return Page[ModelSummary](rows=summaries, total=total, truncated=truncated)


async def query_versions(conn: AsyncConnection, q: TrackingQuery) -> Page[ModelVersionRow]:
    stmt = select(model_versions)
    if q.source_id:
        stmt = stmt.where(model_versions.c.source_id == q.source_id)
    if q.model:
        stmt = stmt.where(model_versions.c.model == q.model)
    if q.version:
        stmt = stmt.where(model_versions.c.version == q.version)
    if q.run_key:
        stmt = stmt.where(model_versions.c.run_key == q.run_key)
    if q.since:
        stmt = stmt.where(model_versions.c.created_at >= q.since.isoformat())

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total: int = int((await conn.scalar(count_stmt)) or 0)

    stmt = stmt.order_by(
        model_versions.c.created_at.desc(),
        model_versions.c.source_id.desc(),
        model_versions.c.model.desc(),
        model_versions.c.version.desc(),
    )
    stmt = stmt.limit(q.limit).offset(q.offset)

    rows = (await conn.execute(stmt)).mappings().all()
    version_rows = [
        ModelVersionRow(
            source_id=r["source_id"],
            model=r["model"],
            version=r["version"],
            run_key=r["run_key"],
            aliases=json.loads(r["aliases"]),
            created=_require_dt(r["created_at"]),
            evaluation=r["evaluation"],
        )
        for r in rows
    ]
    truncated = total > q.offset + len(version_rows)
    return Page[ModelVersionRow](rows=version_rows, total=total, truncated=truncated)


async def query_runs(conn: AsyncConnection, q: TrackingQuery) -> Page[RunRow]:
    stmt = select(tracked_runs)
    if q.source_id:
        stmt = stmt.where(tracked_runs.c.source_id == q.source_id)
    if q.run_key:
        stmt = stmt.where(tracked_runs.c.key == q.run_key)
    if q.since:
        stmt = stmt.where(tracked_runs.c.started_at >= q.since.isoformat())

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total: int = int((await conn.scalar(count_stmt)) or 0)

    stmt = stmt.order_by(
        tracked_runs.c.started_at.desc(),
        tracked_runs.c.source_id.desc(),
        tracked_runs.c.key.desc(),
    )
    stmt = stmt.limit(q.limit).offset(q.offset)

    rows = (await conn.execute(stmt)).mappings().all()
    run_rows = [
        RunRow(
            source_id=r["source_id"],
            key=r["key"],
            experiment=r["experiment"],
            status=r["status"],
            start=_parse_dt(r["started_at"]),
            end=_parse_dt(r["ended_at"]),
            params=json.loads(r["params"]),
            tags=json.loads(r["tags"]),
            git_commit=r["git_commit"],
            datasets=json.loads(r["datasets"]),
        )
        for r in rows
    ]
    truncated = total > q.offset + len(run_rows)
    return Page[RunRow](rows=run_rows, total=total, truncated=truncated)


async def query_metrics(conn: AsyncConnection, q: TrackingQuery) -> Page[MetricPoint]:
    if not q.run_key:
        raise DomainError("tracking_bad_query", "Querying metrics requires run_key.", status=422)

    base = select(run_metrics).where(run_metrics.c.run_key == q.run_key)
    if q.source_id:
        base = base.where(run_metrics.c.source_id == q.source_id)
    if q.metric:
        base = base.where(run_metrics.c.metric == q.metric)
    if q.since:
        base = base.where(run_metrics.c.time >= q.since.isoformat())

    if q.latest:
        # Window function: partition by (source_id, run_key, metric), highest step, newest time on tie
        subq = select(
            run_metrics,
            func.row_number()
            .over(
                partition_by=[run_metrics.c.source_id, run_metrics.c.run_key, run_metrics.c.metric],
                order_by=[run_metrics.c.step.desc(), run_metrics.c.time.desc()],
            )
            .label("rn"),
        ).where(run_metrics.c.run_key == q.run_key)
        if q.source_id:
            subq = subq.where(run_metrics.c.source_id == q.source_id)
        if q.metric:
            subq = subq.where(run_metrics.c.metric == q.metric)
        if q.since:
            subq = subq.where(run_metrics.c.time >= q.since.isoformat())

        subq_aliased = subq.subquery()
        stmt = (
            select(subq_aliased)
            .where(subq_aliased.c.rn == 1)
            .order_by(subq_aliased.c.time.desc(), subq_aliased.c.metric.asc())
        )
        count_stmt = select(func.count()).select_from(
            select(subq_aliased).where(subq_aliased.c.rn == 1).subquery()
        )
        total: int = int((await conn.scalar(count_stmt)) or 0)
        stmt = stmt.limit(q.limit).offset(q.offset)
        rows = (await conn.execute(stmt)).mappings().all()
    else:
        count_stmt = select(func.count()).select_from(base.subquery())
        total = int((await conn.scalar(count_stmt)) or 0)
        stmt = base.order_by(
            run_metrics.c.time.desc(),
            run_metrics.c.step.desc(),
            run_metrics.c.metric.asc(),
            run_metrics.c.source_id.desc(),
        )
        stmt = stmt.limit(q.limit).offset(q.offset)
        rows = (await conn.execute(stmt)).mappings().all()

    points = [
        MetricPoint(
            source_id=r["source_id"],
            run_key=r["run_key"],
            metric=r["metric"],
            step=r["step"],
            value=float(r["value"]),
            time=_require_dt(r["time"]),
        )
        for r in rows
    ]
    truncated = total > q.offset + len(points)
    return Page[MetricPoint](rows=points, total=total, truncated=truncated)


async def query_deployments(conn: AsyncConnection, q: TrackingQuery) -> Page[DeploymentRow]:
    stmt = select(deployments)
    if q.source_id:
        stmt = stmt.where(deployments.c.source_id == q.source_id)
    if q.model:
        stmt = stmt.where(deployments.c.model == q.model)
    if q.since:
        stmt = stmt.where(deployments.c.seen_at >= q.since.isoformat())

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total: int = int((await conn.scalar(count_stmt)) or 0)

    stmt = stmt.order_by(
        deployments.c.seen_at.desc(),
        deployments.c.source_id.desc(),
        deployments.c.model.desc(),
        deployments.c.alias.desc(),
    )
    stmt = stmt.limit(q.limit).offset(q.offset)

    rows = (await conn.execute(stmt)).mappings().all()
    dep_rows = [
        DeploymentRow(
            source_id=r["source_id"],
            model=r["model"],
            alias=r["alias"],
            from_version=r["from_version"],
            to_version=r["to_version"],
            seen_at=_require_dt(r["seen_at"]),
        )
        for r in rows
    ]
    truncated = total > q.offset + len(dep_rows)
    return Page[DeploymentRow](rows=dep_rows, total=total, truncated=truncated)


async def query_ci_runs(conn: AsyncConnection, q: TrackingQuery) -> Page[CiRunRow]:
    stmt = select(ci_runs)
    if q.source_id:
        stmt = stmt.where(ci_runs.c.source_id == q.source_id)
    if q.since:
        stmt = stmt.where(ci_runs.c.started_at >= q.since.isoformat())

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total: int = int((await conn.scalar(count_stmt)) or 0)

    stmt = stmt.order_by(
        ci_runs.c.started_at.desc(),
        ci_runs.c.source_id.desc(),
        ci_runs.c.key.desc(),
    )
    stmt = stmt.limit(q.limit).offset(q.offset)

    rows = (await conn.execute(stmt)).mappings().all()
    ci_rows = [
        CiRunRow(
            source_id=r["source_id"],
            key=r["key"],
            workflow=r["workflow"],
            status=r["status"],
            start=_require_dt(r["started_at"]),
            duration_s=r["duration_s"],
            commit=r["commit_sha"],
            url=r["url"],
        )
        for r in rows
    ]
    truncated = total > q.offset + len(ci_rows)
    return Page[CiRunRow](rows=ci_rows, total=total, truncated=truncated)


async def query_evaluations(conn: AsyncConnection, q: TrackingQuery) -> Page[EvaluationRecord]:
    stmt = select(evaluations)
    if q.source_id:
        stmt = stmt.where(evaluations.c.source_id == q.source_id)
    if q.model:
        stmt = stmt.where(evaluations.c.model == q.model)
    if q.version:
        stmt = stmt.where(evaluations.c.version == q.version)
    if q.run_key:
        stmt = stmt.where(evaluations.c.run_key == q.run_key)
    if q.since:
        stmt = stmt.where(evaluations.c.fetched_at >= q.since.isoformat())

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total: int = int((await conn.scalar(count_stmt)) or 0)

    stmt = stmt.order_by(
        evaluations.c.fetched_at.desc(),
        evaluations.c.source_id.desc(),
        evaluations.c.model.desc(),
        evaluations.c.version.desc(),
    )
    stmt = stmt.limit(q.limit).offset(q.offset)

    rows = (await conn.execute(stmt)).mappings().all()
    eval_rows = [
        EvaluationRecord(
            source_id=r["source_id"],
            model=r["model"],
            version=r["version"],
            run_key=r["run_key"],
            schema_version=r["schema_version"],
            payload=json.loads(r["payload"]),
            fetched_at=_require_dt(r["fetched_at"]),
        )
        for r in rows
    ]
    truncated = total > q.offset + len(eval_rows)
    return Page[EvaluationRecord](rows=eval_rows, total=total, truncated=truncated)
