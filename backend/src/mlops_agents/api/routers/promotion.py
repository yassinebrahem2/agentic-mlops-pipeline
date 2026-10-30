"""Promotion router (Task 2, #496)."""

from __future__ import annotations

from fastapi import APIRouter

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.errors import DomainError
from mlops_agents.promotion.contract import ReportView

router = APIRouter(tags=["promotion"])


@router.get(
    "/projects/{project_id}/reports/{report_id}",
    response_model=ReportView,
)
async def get_report(
    project: ProjectDep,
    services: ServicesDep,
    report_id: str,
) -> ReportView:
    report = await services.reports.get(project.id, report_id)
    if report is None:
        raise DomainError("report_not_found", f"Report {report_id} not found", status=404)
    return report
