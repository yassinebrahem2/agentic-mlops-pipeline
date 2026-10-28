"""Tracking sources and records routes (I16)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.errors import DomainError
from mlops_agents.tracking.constants import QUERY_LIMIT_DEFAULT, QUERY_LIMIT_MAX
from mlops_agents.tracking.contract import (
    CiRunRow,
    DeploymentRow,
    EvaluationRecord,
    MetricPoint,
    ModelSummary,
    ModelVersionRow,
    Page,
    RunRow,
    SourceType,
    TrackingQuery,
    TrackingSourceView,
)

router = APIRouter(tags=["tracking"])


class CreateSourceBody(BaseModel):
    type: SourceType
    settings: dict[str, Any]
    auth: Literal["none", "credential"] = "none"


class SetCredentialBody(BaseModel):
    credential: str


class SyncBody(BaseModel):
    source_id: str | None = None


def _check_paging(limit: int, offset: int) -> None:
    if limit > QUERY_LIMIT_MAX or limit < 1:
        raise DomainError(
            "tracking_bad_query",
            f"Query limit must be between 1 and {QUERY_LIMIT_MAX}.",
            status=422,
        )
    if offset < 0:
        raise DomainError("tracking_bad_query", "Query offset must be non-negative.", status=422)


@router.get("/projects/{project_id}/tracking/sources", response_model=list[TrackingSourceView])
async def list_sources(
    project: ProjectDep,
    services: ServicesDep,
) -> list[TrackingSourceView]:
    return await services.tracking.sources(project.id)


@router.post("/projects/{project_id}/tracking/sources", response_model=TrackingSourceView)
async def create_source(
    project: ProjectDep,
    body: CreateSourceBody,
    services: ServicesDep,
) -> TrackingSourceView:
    return await services.tracking.create_source(
        project_id=project.id,
        type=body.type,
        settings=body.settings,
        auth=body.auth,
    )


@router.put(
    "/projects/{project_id}/tracking/sources/{source_id}/credential",
    response_model=TrackingSourceView,
)
async def set_credential(
    project: ProjectDep,
    source_id: str,
    body: SetCredentialBody,
    services: ServicesDep,
) -> TrackingSourceView:
    return await services.tracking.set_credential(
        project_id=project.id,
        source_id=source_id,
        credential=body.credential,
    )


@router.post(
    "/projects/{project_id}/tracking/sync",
    status_code=status.HTTP_202_ACCEPTED,
    response_class=Response,
)
async def sync_sources(
    project: ProjectDep,
    services: ServicesDep,
    body: SyncBody | None = None,
) -> Response:
    source_id = body.source_id if body else None
    await services.tracking.sync(project.id, source_id=source_id)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.get("/projects/{project_id}/tracking/models", response_model=Page[ModelSummary])
async def get_models(
    project: ProjectDep,
    services: ServicesDep,
    limit: int = Query(default=QUERY_LIMIT_DEFAULT),
    offset: int = Query(default=0),
) -> Page[ModelSummary]:
    _check_paging(limit, offset)
    q = TrackingQuery(what="models", limit=limit, offset=offset)
    return await services.tracking.query(project.id, q)  # type: ignore[return-value]


@router.get("/projects/{project_id}/tracking/versions", response_model=Page[ModelVersionRow])
async def get_versions(
    project: ProjectDep,
    services: ServicesDep,
    model: str | None = Query(default=None),
    source_id: str | None = Query(default=None),
    limit: int = Query(default=QUERY_LIMIT_DEFAULT),
    offset: int = Query(default=0),
) -> Page[ModelVersionRow]:
    _check_paging(limit, offset)
    q = TrackingQuery(what="versions", model=model, source_id=source_id, limit=limit, offset=offset)
    return await services.tracking.query(project.id, q)  # type: ignore[return-value]


@router.get("/projects/{project_id}/tracking/runs", response_model=Page[RunRow])
async def get_runs(
    project: ProjectDep,
    services: ServicesDep,
    run_key: str | None = Query(default=None),
    source_id: str | None = Query(default=None),
    limit: int = Query(default=QUERY_LIMIT_DEFAULT),
    offset: int = Query(default=0),
) -> Page[RunRow]:
    _check_paging(limit, offset)
    q = TrackingQuery(what="runs", run_key=run_key, source_id=source_id, limit=limit, offset=offset)
    return await services.tracking.query(project.id, q)  # type: ignore[return-value]


@router.get("/projects/{project_id}/tracking/ci-runs", response_model=Page[CiRunRow])
async def get_ci_runs(
    project: ProjectDep,
    services: ServicesDep,
    source_id: str | None = Query(default=None),
    limit: int = Query(default=QUERY_LIMIT_DEFAULT),
    offset: int = Query(default=0),
) -> Page[CiRunRow]:
    _check_paging(limit, offset)
    q = TrackingQuery(what="ci_runs", source_id=source_id, limit=limit, offset=offset)
    return await services.tracking.query(project.id, q)  # type: ignore[return-value]


@router.get("/projects/{project_id}/tracking/deployments", response_model=Page[DeploymentRow])
async def get_deployments(
    project: ProjectDep,
    services: ServicesDep,
    model: str | None = Query(default=None),
    source_id: str | None = Query(default=None),
    limit: int = Query(default=QUERY_LIMIT_DEFAULT),
    offset: int = Query(default=0),
) -> Page[DeploymentRow]:
    _check_paging(limit, offset)
    q = TrackingQuery(
        what="deployments", model=model, source_id=source_id, limit=limit, offset=offset
    )
    return await services.tracking.query(project.id, q)  # type: ignore[return-value]


@router.get("/projects/{project_id}/tracking/metrics", response_model=Page[MetricPoint])
async def get_metrics(
    project: ProjectDep,
    services: ServicesDep,
    run_key: str | None = Query(default=None),
    metric: str | None = Query(default=None),
    latest: bool = Query(default=False),
    source_id: str | None = Query(default=None),
    limit: int = Query(default=QUERY_LIMIT_DEFAULT),
    offset: int = Query(default=0),
) -> Page[MetricPoint]:
    _check_paging(limit, offset)
    if run_key is None:
        raise DomainError("tracking_bad_query", "Querying metrics requires run_key.", status=422)
    q = TrackingQuery(
        what="metrics",
        run_key=run_key,
        metric=metric,
        latest=latest,
        source_id=source_id,
        limit=limit,
        offset=offset,
    )
    return await services.tracking.query(project.id, q)  # type: ignore[return-value]


@router.get("/projects/{project_id}/tracking/evaluations", response_model=Page[EvaluationRecord])
async def get_evaluations(
    project: ProjectDep,
    services: ServicesDep,
    model: str | None = Query(default=None),
    version: str | None = Query(default=None),
    source_id: str | None = Query(default=None),
    limit: int = Query(default=QUERY_LIMIT_DEFAULT),
    offset: int = Query(default=0),
) -> Page[EvaluationRecord]:
    _check_paging(limit, offset)
    q = TrackingQuery(
        what="evaluations",
        model=model,
        version=version,
        source_id=source_id,
        limit=limit,
        offset=offset,
    )
    return await services.tracking.query(project.id, q)  # type: ignore[return-value]
