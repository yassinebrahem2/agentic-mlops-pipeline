"""Tracking sources and records routes (I16)."""

from __future__ import annotations

from typing import Any, Literal, cast

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field

from mlops_agents.api.deps import ProjectDep, ServicesDep
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
    credential: str = Field(min_length=1)


class SyncBody(BaseModel):
    source_id: str | None = None


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
    limit: int = Query(default=QUERY_LIMIT_DEFAULT, ge=1, le=QUERY_LIMIT_MAX),
    offset: int = Query(default=0, ge=0),
) -> Page[ModelSummary]:
    q = TrackingQuery(what="models", limit=limit, offset=offset)
    result = await services.tracking.query(project.id, q)
    return cast(Page[ModelSummary], result)


@router.get("/projects/{project_id}/tracking/versions", response_model=Page[ModelVersionRow])
async def get_versions(
    project: ProjectDep,
    services: ServicesDep,
    model: str | None = Query(default=None),
    source_id: str | None = Query(default=None),
    limit: int = Query(default=QUERY_LIMIT_DEFAULT, ge=1, le=QUERY_LIMIT_MAX),
    offset: int = Query(default=0, ge=0),
) -> Page[ModelVersionRow]:
    q = TrackingQuery(what="versions", model=model, source_id=source_id, limit=limit, offset=offset)
    result = await services.tracking.query(project.id, q)
    return cast(Page[ModelVersionRow], result)


@router.get("/projects/{project_id}/tracking/runs", response_model=Page[RunRow])
async def get_runs(
    project: ProjectDep,
    services: ServicesDep,
    run_key: str | None = Query(default=None),
    limit: int = Query(default=QUERY_LIMIT_DEFAULT, ge=1, le=QUERY_LIMIT_MAX),
    offset: int = Query(default=0, ge=0),
) -> Page[RunRow]:
    q = TrackingQuery(what="runs", run_key=run_key, limit=limit, offset=offset)
    result = await services.tracking.query(project.id, q)
    return cast(Page[RunRow], result)


@router.get("/projects/{project_id}/tracking/ci-runs", response_model=Page[CiRunRow])
async def get_ci_runs(
    project: ProjectDep,
    services: ServicesDep,
    limit: int = Query(default=QUERY_LIMIT_DEFAULT, ge=1, le=QUERY_LIMIT_MAX),
    offset: int = Query(default=0, ge=0),
) -> Page[CiRunRow]:
    q = TrackingQuery(what="ci_runs", limit=limit, offset=offset)
    result = await services.tracking.query(project.id, q)
    return cast(Page[CiRunRow], result)


@router.get("/projects/{project_id}/tracking/deployments", response_model=Page[DeploymentRow])
async def get_deployments(
    project: ProjectDep,
    services: ServicesDep,
    model: str | None = Query(default=None),
    source_id: str | None = Query(default=None),
    limit: int = Query(default=QUERY_LIMIT_DEFAULT, ge=1, le=QUERY_LIMIT_MAX),
    offset: int = Query(default=0, ge=0),
) -> Page[DeploymentRow]:
    q = TrackingQuery(
        what="deployments", model=model, source_id=source_id, limit=limit, offset=offset
    )
    result = await services.tracking.query(project.id, q)
    return cast(Page[DeploymentRow], result)


@router.get("/projects/{project_id}/tracking/metrics", response_model=Page[MetricPoint])
async def get_metrics(
    project: ProjectDep,
    services: ServicesDep,
    run_key: str | None = Query(default=None),
    metric: str | None = Query(default=None),
    latest: bool = Query(default=False),
    limit: int = Query(default=QUERY_LIMIT_DEFAULT, ge=1, le=QUERY_LIMIT_MAX),
    offset: int = Query(default=0, ge=0),
) -> Page[MetricPoint]:
    q = TrackingQuery(
        what="metrics",
        run_key=run_key,
        metric=metric,
        latest=latest,
        limit=limit,
        offset=offset,
    )
    result = await services.tracking.query(project.id, q)
    return cast(Page[MetricPoint], result)


@router.get("/projects/{project_id}/tracking/evaluations", response_model=Page[EvaluationRecord])
async def get_evaluations(
    project: ProjectDep,
    services: ServicesDep,
    model: str | None = Query(default=None),
    version: str | None = Query(default=None),
    limit: int = Query(default=QUERY_LIMIT_DEFAULT, ge=1, le=QUERY_LIMIT_MAX),
    offset: int = Query(default=0, ge=0),
) -> Page[EvaluationRecord]:
    q = TrackingQuery(
        what="evaluations",
        model=model,
        version=version,
        limit=limit,
        offset=offset,
    )
    result = await services.tracking.query(project.id, q)
    return cast(Page[EvaluationRecord], result)
