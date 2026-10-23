from __future__ import annotations

from pathlib import Path

import httpx2
import pytest

from mlops_agents.errors import DomainError
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.session_env import build_session_env
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


async def test_agent_validation(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    # Definition with chat: false
    extra_dir = tmp_path / "extra_defs"
    extra_dir.mkdir()
    (extra_dir / "no-chat.md").write_text(
        "---\nname: no-chat\ndisplay_name: No Chat\ndescription: No chat agent\nchat: false\n---\nPrompt",
        encoding="utf-8",
    )

    env = await build_session_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        extra_dirs=[extra_dir],
    )

    manager = env.manager

    project_id = env.project_id

    # 1. Unknown agent -> 404
    with pytest.raises(DomainError) as exc_404:
        await manager.send(project_id, "nonexistent", "Hello")
    assert exc_404.value.status == 404
    assert exc_404.value.code == "agent_not_found"

    with pytest.raises(DomainError) as exc_404_get:
        await manager.get(project_id, "nonexistent")
    assert exc_404_get.value.status == 404

    with pytest.raises(DomainError) as exc_404_stop:
        await manager.stop(project_id, "nonexistent")
    assert exc_404_stop.value.status == 404

    with pytest.raises(DomainError) as exc_404_sub:
        sub = manager.subscribe(project_id, "nonexistent")
        await anext(sub)
    assert exc_404_sub.value.status == 404

    # 2. chat: false -> 409
    with pytest.raises(DomainError) as exc_409:
        await manager.send(project_id, "no-chat", "Hello")
    assert exc_409.value.status == 409
    assert exc_409.value.code == "agent_cannot_chat"

    with pytest.raises(DomainError) as exc_409_get:
        await manager.get(project_id, "no-chat")
    assert exc_409_get.value.status == 409

    # 3. empty text -> 422
    with pytest.raises(DomainError) as exc_422_empty:
        await manager.send(project_id, "orchestrator", "")
    assert exc_422_empty.value.status == 422
    assert exc_422_empty.value.code == "message_empty"

    with pytest.raises(DomainError) as exc_422_ws:
        await manager.send(project_id, "orchestrator", "   \n\t  ")
    assert exc_422_ws.value.status == 422

    await manager.close()


async def test_unknown_project_raises_404_and_never_creates_db(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    env = await build_session_env(
        tmp_path, paths, databases, keystore, fake_openai, model_http, git_env
    )
    manager = env.manager
    unknown_id = "prj_nonexistent"
    db_path = paths.project_db(unknown_id)
    assert not db_path.exists()

    # 1. get
    with pytest.raises(DomainError) as exc_get:
        await manager.get(unknown_id, "orchestrator")
    assert exc_get.value.status == 404
    assert not db_path.exists()

    # 2. send
    with pytest.raises(DomainError) as exc_send:
        await manager.send(unknown_id, "orchestrator", "Hello")
    assert exc_send.value.status == 404
    assert not db_path.exists()

    # 3. stop
    with pytest.raises(DomainError) as exc_stop:
        await manager.stop(unknown_id, "orchestrator")
    assert exc_stop.value.status == 404
    assert not db_path.exists()

    # 4. subscribe
    sub = manager.subscribe(unknown_id, "orchestrator")
    with pytest.raises(DomainError) as exc_sub:
        await anext(sub)
    assert exc_sub.value.status == 404
    assert not db_path.exists()

    # 5. close_project
    with pytest.raises(DomainError) as exc_close:
        await manager.close_project(unknown_id)
    assert exc_close.value.status == 404
    assert not db_path.exists()

    await manager.close()
