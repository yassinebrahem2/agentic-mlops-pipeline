"""Task routes: discard task (spec §6)."""

from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Query, Response, status
from sqlalchemy.exc import SQLAlchemyError

from mlops_agents.api.deps import ProjectDep, ServicesDep
from mlops_agents.errors import DomainError

router = APIRouter(tags=["tasks"])
logger = logging.getLogger(__name__)


async def _discard_task(services: ServicesDep, project_id: str, task_id: str) -> None:
    task = await services.tasks.view(project_id, task_id)
    states = await services.sessions.turn_states(project_id)
    if task.agent in states:
        state = states[task.agent]
        raise DomainError(
            "task_turn_active",
            f"Cannot discard task while agent '{task.agent}' has a turn {state}.",
            status=status.HTTP_409_CONFLICT,
        )
    await services.tasks.end(project_id, task_id, status="discarded")


@router.post(
    "/projects/{project_id}/tasks/{task_id}/discard",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def discard_project_task(
    project_id: str,
    task_id: str,
    services: ServicesDep,
    _project: ProjectDep,
) -> Response:
    await _discard_task(services, project_id, task_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/tasks/{task_id}/discard",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def discard_task(
    task_id: str,
    services: ServicesDep,
    project_id: str | None = Query(default=None),
) -> Response:
    if project_id is None:
        projects = await services.workspace.list_projects()
        for proj in projects:
            try:
                await services.tasks.view(proj.id, task_id)
                project_id = proj.id
                break
            except DomainError:
                continue
            except (sqlite3.DatabaseError, SQLAlchemyError, OSError):
                # One unreadable project must not hide the task in another one.
                logger.exception("Skipped project %s while looking for task %s", proj.id, task_id)
                continue

    if project_id is None:
        raise DomainError("task_not_found", f"Task {task_id} not found.", status=404)

    await _discard_task(services, project_id, task_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
