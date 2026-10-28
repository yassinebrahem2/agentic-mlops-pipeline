"""Project data connection routes (spec §4)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.connections.contract import ConnectionView, Findings, TableRef

router = APIRouter(tags=["connections"])


class InspectConnection(BaseModel):
    kind: str
    settings: dict[str, Any]


class CreateConnection(BaseModel):
    kind: str
    name: str
    settings: dict[str, Any]


class UpdateConnection(BaseModel):
    settings: dict[str, Any]


@router.post(
    "/projects/{project_id}/connections/inspect",
    response_model=Findings,
)
async def inspect_connection(
    project_id: str,
    body: InspectConnection,
    services: ServicesDep,
    _project: ProjectDep,
) -> Findings:
    return await services.connections.inspect(project_id, body.kind, body.settings)


@router.post(
    "/projects/{project_id}/connections",
    status_code=status.HTTP_201_CREATED,
    response_model=ConnectionView,
)
async def create_connection(
    project_id: str,
    body: CreateConnection,
    services: ServicesDep,
    _project: ProjectDep,
) -> ConnectionView:
    return await services.connections.create(project_id, body.kind, body.name, body.settings)


@router.get(
    "/projects/{project_id}/connections",
    response_model=list[ConnectionView],
)
async def list_connections(
    project_id: str,
    services: ServicesDep,
    _project: ProjectDep,
) -> list[ConnectionView]:
    return await services.connections.list(project_id)


@router.patch(
    "/projects/{project_id}/connections/{connection_id}",
    response_model=ConnectionView,
)
async def update_connection(
    project_id: str,
    connection_id: str,
    body: UpdateConnection,
    services: ServicesDep,
    _project: ProjectDep,
) -> ConnectionView:
    return await services.connections.update(project_id, connection_id, body.settings)


@router.delete(
    "/projects/{project_id}/connections/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_connection(
    project_id: str,
    connection_id: str,
    services: ServicesDep,
    _project: ProjectDep,
) -> Response:
    await services.connections.delete(project_id, connection_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/projects/{project_id}/connections/{connection_id}/tables",
    response_model=list[TableRef],
)
async def list_connection_tables(
    project_id: str,
    connection_id: str,
    services: ServicesDep,
    _project: ProjectDep,
) -> list[TableRef]:
    return await services.connections.tables(project_id, connection_id)
