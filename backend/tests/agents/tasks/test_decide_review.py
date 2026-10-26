from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import update

from mlops_agents.agents.tasks.service import TasksService
from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.errors import DomainError
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import tasks
from tests.agents.tasks.helpers import create_session, setup_service
from tests.conftest import MemoryKeyStore


async def _local_review_task(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> tuple[TasksService, str, TaskRef]:
    service, _, _, project_id, _ = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Decide")
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == task.id)
            .values(status="in_review", review_kind="local")
        )
    return service, project_id, task


async def test_decide_review_runs_the_step_under_the_task_lock_then_ends_the_task(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id, task = await _local_review_task(
        tmp_path, paths, databases, keystore, git_env
    )
    seen: list[str] = []

    async def step(ref: TaskRef) -> None:
        seen.append(ref.id)
        # Any other task call waits for the lock this decision holds.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(service.has_commits_beyond_base(project_id, ref), 0.3)

    view = await service.decide_review(project_id, task.id, step, "closed")

    assert seen == [task.id]
    assert view.status == "closed"
    assert (await service.view(project_id, task.id)).status == "closed"


async def test_decide_review_leaves_the_task_in_review_when_the_step_fails(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id, task = await _local_review_task(
        tmp_path, paths, databases, keystore, git_env
    )

    async def failing_step(ref: TaskRef) -> None:
        raise DomainError("step_failed", "The step failed.", status=503)

    with pytest.raises(DomainError) as err:
        await service.decide_review(project_id, task.id, failing_step, "closed")
    assert err.value.code == "step_failed"
    assert (await service.view(project_id, task.id)).status == "in_review"


async def test_decide_review_refuses_a_task_not_in_local_review_with_409(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id, task = await _local_review_task(
        tmp_path, paths, databases, keystore, git_env
    )
    ran: list[str] = []

    async def step(ref: TaskRef) -> None:
        ran.append(ref.id)

    await service.decide_review(project_id, task.id, step, "closed")
    with pytest.raises(DomainError) as err:
        await service.decide_review(project_id, task.id, step, "closed")
    assert err.value.status == 409
    assert ran == [task.id]
