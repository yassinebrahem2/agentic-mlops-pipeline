from __future__ import annotations

import json
from pathlib import Path

import pytest

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.errors import DomainError
from mlops_agents.sandbox.contract import (
    CommandResult,
    DockerStatus,
    Sandbox,
)
from mlops_agents.sandbox.service import SandboxService
from tests.sandbox.fakes import FakeDockerRunner, FakeEventLog, MemorySandboxProjects


async def test_service_implements_sandbox_protocol(tmp_path: Path) -> None:
    version_json = json.dumps({"Client": {"Version": "25.0.0"}, "Server": {"Version": "25.0.0"}})
    service = SandboxService(
        FakeDockerRunner(stdout=version_json),
        projects=MemorySandboxProjects(),
        events=FakeEventLog(),
    )
    # Static protocol check:
    sandbox: Sandbox = service
    assert {"docker", "environment", "rebuild", "run", "remove_task", "paused", "close"} <= set(
        dir(sandbox)
    )

    status = await service.docker()
    assert status == DockerStatus(available=True, version="25.0.0", detail=None)

    with pytest.raises(DomainError):
        await service.environment("prj_123")

    with pytest.raises(DomainError):
        await service.rebuild("prj_123")

    task = TaskRef(
        id="tsk_1",
        title="Test task",
        branch="agent/task-1",
        worktree=tmp_path,
        status="active",
    )
    res = await service.run("prj_123", task, "echo hi", 60)
    assert isinstance(res, CommandResult)
    assert res.exit_code == 0

    # remove_task and close are safe no-ops
    await service.remove_task("prj_123", "tsk_1")
    await service.close()
