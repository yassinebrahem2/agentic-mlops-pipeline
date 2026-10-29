"""The table-detail route (spec §6): a table's profile from the data-summary cache."""

from __future__ import annotations

from fastapi import APIRouter

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.connections.contract import TableDetail
from mlops_agents.errors import DomainError

router = APIRouter(tags=["data"])


@router.get(
    "/projects/{project_id}/connections/{connection_id}/tables/{table}",
    response_model=TableDetail,
)
async def get_table_detail(
    project_id: str,
    connection_id: str,
    table: str,
    services: ServicesDep,
    _project: ProjectDep,
) -> TableDetail:
    # The messages are fixed: the IDs and the table name come from the URL.
    views = await services.connections.list(project_id)
    view = next((v for v in views if v.id == connection_id), None)
    if view is None:
        raise DomainError("connection_not_found", "This connection does not exist.", status=404)
    if view.status == "unreachable":
        raise DomainError(
            "connection_unreachable",
            f"The connection is unreachable: {view.status_detail or 'no reason given'}",
            status=409,
        )
    # Only a name from the listing goes further, so the URL's text never reaches SQL.
    refs = await services.connections.tables(project_id, connection_id)
    ref = next((r for r in refs if r.name == table), None)
    if ref is None:
        raise DomainError(
            "table_not_found", "This table does not exist in the connection.", status=404
        )
    return await services.data_summaries.table_detail(project_id, connection_id, ref)
