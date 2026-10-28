"""The read side of tasks: views, open tasks and a session's current task."""

from __future__ import annotations

from pathlib import Path

import pytest

from mlops_agents.agents.tasks import queries
from mlops_agents.errors import DomainError
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.tasks.helpers import create_session, setup_service
from tests.conftest import MemoryKeyStore


async def test_view_of_an_unknown_task_is_task_not_found(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    _, _, _, project_id, _ = await setup_service(tmp_path, paths, databases, keystore, git_env)
    engine = await databases.project(project_id)

    with pytest.raises(DomainError) as err:
        await queries.view(engine, "tsk_missing")

    assert err.value.code == "task_not_found"
    assert err.value.status == 404


async def test_view_and_open_tasks_read_the_stored_rows(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, _, project_id, _ = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await create_session(databases, project_id, "ses_1", agent="coder")
    first = await service.start(project_id, "ses_1", "coder", "First")
    second = await service.start(project_id, "ses_1", "coder", "Second")
    engine = await databases.project(project_id)

    view = await queries.view(engine, first.id)
    assert (view.id, view.agent, view.title, view.status) == (first.id, "coder", "First", "active")
    assert [t.id for t in await queries.open_tasks(engine)] == [first.id, second.id]


async def test_current_drops_a_stale_cache_entry_and_carries_the_pending_note(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    service, _, _, project_id, _ = await setup_service(
        tmp_path, paths, databases, keystore, git_env
    )
    await create_session(databases, project_id, "ses_1", agent="coder")
    task = await service.start(project_id, "ses_1", "coder", "Current")
    engine = await databases.project(project_id)
    current_tasks = {(project_id, "ses_1"): "tsk_gone"}
    pending_notes = {(project_id, task.id): "Worktree recreated."}

    found = await queries.current(engine, project_id, "ses_1", current_tasks, pending_notes)

    assert found is not None
    assert found.id == task.id
    assert found.pending_note == "Worktree recreated."
    assert current_tasks == {(project_id, "ses_1"): task.id}


async def test_current_is_none_without_an_open_task(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    git_env: dict[str, str],
) -> None:
    _, _, _, project_id, _ = await setup_service(tmp_path, paths, databases, keystore, git_env)
    await create_session(databases, project_id, "ses_1", agent="coder")
    engine = await databases.project(project_id)
    current_tasks: dict[tuple[str, str], str] = {}

    assert await queries.current(engine, project_id, "ses_1", current_tasks, {}) is None
    assert current_tasks == {}
