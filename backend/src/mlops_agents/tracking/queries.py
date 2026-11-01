"""Tracking queries: querying models, versions, runs, metrics, deployments, ci-runs, evaluations."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import RowMapping, Select, func, select, tuple_
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
    TrackingQuery,
)


def _parse_dt(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    return datetime.fromisoformat(raw)


def _require_dt(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


async def _page_query[T](
    conn: AsyncConnection,
    base_stmt: Select[Any],
    order_by_clauses: Sequence[Any],
    mapper: Callable[[RowMapping], T],
    limit: int,
    offset: int,
) -> Page[T]:
    count_stmt = select(func.count()).select_from(base_stmt.subquery())
    total: int = int((await conn.scalar(count_stmt)) or 0)
    paged_stmt = base_stmt.order_by(*order_by_clauses).limit(limit).offset(offset)
    result = await conn.execute(paged_stmt)
    rows = [mapper(row) for row in result.mappings().all()]
    truncated = total > offset + len(rows)
    return Page[T](rows=rows, total=total, truncated=truncated)


async def query_models(
    conn: AsyncConnection,
    q: TrackingQuery,
    candidate_aliases_fn: Callable[[str], Sequence[str]] | None = None,
) -> Page[ModelSummary]:
    stmt = select(tracked_models)
    if q.source_id:
        stmt = stmt.where(tracked_models.c.source_id == q.source_id)
    if q.model:
        stmt = stmt.where(tracked_models.c.name == q.model)

    models_page = await _page_query(
        conn,
        stmt,
        [tracked_models.c.name.asc(), tracked_models.c.source_id.asc()],
        lambda r: (str(r["source_id"]), str(r["name"])),
        q.limit,
        q.offset,
    )
    if not models_page.rows:
        return Page[ModelSummary](rows=[], total=models_page.total, truncated=False)

    models_on_page = models_page.rows

    # Fetch all versions for all models on this page in one batch query
    v_stmt = (
        select(
            model_versions.c.source_id,
            model_versions.c.model,
            model_versions.c.version,
            model_versions.c.aliases,
            model_versions.c.created_at,
        )
        .where(tuple_(model_versions.c.source_id, model_versions.c.model).in_(models_on_page))
        .order_by(model_versions.c.created_at.desc(), model_versions.c.version.desc())
    )
    v_results = (await conn.execute(v_stmt)).all()

    versions_by_model: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for v in v_results:
        versions_by_model[(v.source_id, v.model)].append(v)

    summaries: list[ModelSummary] = []
    for src_id, name in models_on_page:
        v_rows = versions_by_model.get((src_id, name), [])
        versions_count = len(v_rows)
        latest_version = v_rows[0].version if v_rows else None
        candidate_aliases = (
            candidate_aliases_fn(name) if candidate_aliases_fn is not None else [PRODUCTION_ALIAS]
        )
        prod_version: str | None = None
        for alias in candidate_aliases:
            for vr in v_rows:
                aliases_list: list[str] = json.loads(vr.aliases)
                if alias in aliases_list:
                    prod_version = vr.version
                    break
            if prod_version is not None:
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

    return Page[ModelSummary](
        rows=summaries, total=models_page.total, truncated=models_page.truncated
    )


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
        stmt = stmt.where(model_versions.c.created_at >= to_iso(q.since))

    order_clauses = [
        model_versions.c.created_at.desc(),
        model_versions.c.source_id.desc(),
        model_versions.c.model.desc(),
        model_versions.c.version.desc(),
    ]

    def _mapper(r: RowMapping) -> ModelVersionRow:
        return ModelVersionRow(
            source_id=r["source_id"],
            model=r["model"],
            version=r["version"],
            run_key=r["run_key"],
            aliases=json.loads(r["aliases"]),
            created=_require_dt(r["created_at"]),
            evaluation=r["evaluation"],
        )

    return await _page_query(conn, stmt, order_clauses, _mapper, q.limit, q.offset)


async def query_runs(conn: AsyncConnection, q: TrackingQuery) -> Page[RunRow]:
    stmt = select(tracked_runs)
    if q.source_id:
        stmt = stmt.where(tracked_runs.c.source_id == q.source_id)
    if q.run_key:
        stmt = stmt.where(tracked_runs.c.key == q.run_key)
    if q.since:
        stmt = stmt.where(tracked_runs.c.started_at >= to_iso(q.since))

    order_clauses = [
        tracked_runs.c.started_at.desc(),
        tracked_runs.c.source_id.desc(),
        tracked_runs.c.key.desc(),
    ]

    def _mapper(r: RowMapping) -> RunRow:
        return RunRow(
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

    return await _page_query(conn, stmt, order_clauses, _mapper, q.limit, q.offset)


async def query_metrics(conn: AsyncConnection, q: TrackingQuery) -> Page[MetricPoint]:
    base = select(run_metrics).where(run_metrics.c.run_key == q.run_key)
    if q.source_id:
        base = base.where(run_metrics.c.source_id == q.source_id)
    if q.metric:
        base = base.where(run_metrics.c.metric == q.metric)
    if q.since:
        base = base.where(run_metrics.c.time >= to_iso(q.since))

    if q.latest:
        subq = select(
            run_metrics,
            func.row_number()
            .over(
                partition_by=[run_metrics.c.source_id, run_metrics.c.run_key, run_metrics.c.metric],
                order_by=[
                    run_metrics.c.step.desc(),
                    run_metrics.c.time.desc(),
                    run_metrics.c.source_id.asc(),
                ],
            )
            .label("rn"),
        ).where(run_metrics.c.run_key == q.run_key)
        if q.source_id:
            subq = subq.where(run_metrics.c.source_id == q.source_id)
        if q.metric:
            subq = subq.where(run_metrics.c.metric == q.metric)
        if q.since:
            subq = subq.where(run_metrics.c.time >= to_iso(q.since))

        subq_aliased = subq.subquery()
        stmt = select(subq_aliased).where(subq_aliased.c.rn == 1)
        order_clauses = [
            subq_aliased.c.time.desc(),
            subq_aliased.c.metric.asc(),
            subq_aliased.c.source_id.asc(),
        ]
    else:
        stmt = base
        order_clauses = [
            run_metrics.c.time.desc(),
            run_metrics.c.step.desc(),
            run_metrics.c.metric.asc(),
            run_metrics.c.source_id.desc(),
        ]

    def _mapper(r: RowMapping) -> MetricPoint:
        return MetricPoint(
            source_id=r["source_id"],
            run_key=r["run_key"],
            metric=r["metric"],
            step=r["step"],
            value=float(r["value"]),
            time=_require_dt(r["time"]),
        )

    return await _page_query(conn, stmt, order_clauses, _mapper, q.limit, q.offset)


async def query_deployments(conn: AsyncConnection, q: TrackingQuery) -> Page[DeploymentRow]:
    stmt = select(deployments)
    if q.source_id:
        stmt = stmt.where(deployments.c.source_id == q.source_id)
    if q.model:
        stmt = stmt.where(deployments.c.model == q.model)
    if q.since:
        stmt = stmt.where(deployments.c.seen_at >= to_iso(q.since))

    order_clauses = [
        deployments.c.seen_at.desc(),
        deployments.c.source_id.desc(),
        deployments.c.model.desc(),
        deployments.c.alias.desc(),
    ]

    def _mapper(r: RowMapping) -> DeploymentRow:
        return DeploymentRow(
            source_id=r["source_id"],
            model=r["model"],
            alias=r["alias"],
            from_version=r["from_version"],
            to_version=r["to_version"],
            seen_at=_require_dt(r["seen_at"]),
            baseline=bool(r.get("baseline", 0)),
        )

    return await _page_query(conn, stmt, order_clauses, _mapper, q.limit, q.offset)


async def query_ci_runs(conn: AsyncConnection, q: TrackingQuery) -> Page[CiRunRow]:
    stmt = select(ci_runs)
    if q.source_id:
        stmt = stmt.where(ci_runs.c.source_id == q.source_id)
    if q.since:
        stmt = stmt.where(ci_runs.c.started_at >= to_iso(q.since))

    order_clauses = [
        ci_runs.c.started_at.desc(),
        ci_runs.c.source_id.desc(),
        ci_runs.c.key.desc(),
    ]

    def _mapper(r: RowMapping) -> CiRunRow:
        return CiRunRow(
            source_id=r["source_id"],
            key=r["key"],
            workflow=r["workflow"],
            status=r["status"],
            start=_require_dt(r["started_at"]),
            duration_s=r["duration_s"],
            commit=r["commit_sha"],
            url=r["url"],
        )

    return await _page_query(conn, stmt, order_clauses, _mapper, q.limit, q.offset)


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
        stmt = stmt.where(evaluations.c.fetched_at >= to_iso(q.since))

    order_clauses = [
        evaluations.c.fetched_at.desc(),
        evaluations.c.source_id.desc(),
        evaluations.c.model.desc(),
        evaluations.c.version.desc(),
    ]

    def _mapper(r: RowMapping) -> EvaluationRecord:
        return EvaluationRecord(
            source_id=r["source_id"],
            model=r["model"],
            version=r["version"],
            run_key=r["run_key"],
            schema_version=r["schema_version"],
            payload=json.loads(r["payload"]),
            fetched_at=_require_dt(r["fetched_at"]),
        )

    return await _page_query(conn, stmt, order_clauses, _mapper, q.limit, q.offset)
