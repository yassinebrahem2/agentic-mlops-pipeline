"""Dependencies routers share: the app's services and the project named in the path."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from mlops_agents.api.services import Services
from mlops_agents.errors import DomainError
from mlops_agents.workspace.models import Project


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


async def get_project(project_id: str, services: ServicesDep) -> Project:
    try:
        services.paths.project_dir(project_id)
    except ValueError:
        # Same answer as an unknown id: a malformed id names no project either.
        raise DomainError("project_not_found", "This project does not exist.", status=404) from None
    return await services.workspace.get(project_id)


ProjectDep = Annotated[Project, Depends(get_project)]
