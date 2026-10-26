from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select, update

from mlops_agents.agents.tasks.contract import ClaimConflict
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from mlops_agents.storage.tables import file_claims, tasks
from tests.agents.tools.test_tasks import create_session, setup_service
from tests.conftest import MemoryKeyStore


async def test_claim_paths_success_and_idempotent(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    session_id = "ses_claims_1"
    agent = "coder"
    await create_session(databases, project_id, session_id, agent=agent)

    task = await service.start(project_id, session_id, agent, "Task 1")

    # Claiming paths for task
    await service.claim(project_id, task, ["app.py", "src/model.py"])

    engine = await databases.project(project_id)
    async with engine.connect() as conn:
        rows = (
            (await conn.execute(select(file_claims).where(file_claims.c.task_id == task.id)))
            .mappings()
            .all()
        )
    claimed_paths = {r["path"] for r in rows}
    assert claimed_paths == {"app.py", "src/model.py"}

    # Re-claiming same paths by same task is idempotent
    await service.claim(project_id, task, ["app.py"])


async def test_claim_conflict_names_other_agent_and_task(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    await create_session(databases, project_id, "ses_coder", agent="coder")
    await create_session(databases, project_id, "ses_reviewer", agent="reviewer")

    task1 = await service.start(project_id, "ses_coder", "coder", "Task 1")
    task2 = await service.start(project_id, "ses_reviewer", "reviewer", "Task 2")

    await service.claim(project_id, task1, ["src/main.py"])

    with pytest.raises(ClaimConflict) as exc_info:
        await service.claim(project_id, task2, ["src/main.py"])

    exc = exc_info.value
    assert exc.agent == "coder"
    assert exc.task_id == task1.id
    assert "src/main.py is claimed by agent 'coder' in task " in str(exc)
    assert task1.id in str(exc)
    assert "Use SendMessage to coordinate." in str(exc)


async def test_claim_conflict_ignores_closed_or_merged_tasks(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, project_id = await setup_service(tmp_path, paths, databases, keystore, git_env)
    await create_session(databases, project_id, "ses_coder", agent="coder")
    await create_session(databases, project_id, "ses_reviewer", agent="reviewer")

    task1 = await service.start(project_id, "ses_coder", "coder", "Task 1")
    task2 = await service.start(project_id, "ses_reviewer", "reviewer", "Task 2")

    await service.claim(project_id, task1, ["src/main.py"])

    # Mark task1 as merged
    engine = await databases.project(project_id)
    async with engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == task1.id).values(status="merged"))

    # Now task2 can claim src/main.py
    await service.claim(project_id, task2, ["src/main.py"])
