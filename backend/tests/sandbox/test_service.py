from __future__ import annotations

import json
from pathlib import Path

import pytest

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.errors import DomainError
from mlops_agents.sandbox.contract import (
    CommandResult,
    DockerStatus,
    EnvironmentStatus,
    Sandbox,
)
from mlops_agents.sandbox.docker import DockerResult, DockerRunner
from mlops_agents.sandbox.service import SandboxService


class FakeRunner(DockerRunner):
    async def run(
        self,
        args: object,
        *,
        timeout_s: float | None = None,
        secrets: object = (),
    ) -> DockerResult:
        version_data = {"Client": {"Version": "25.0.0"}, "Server": {"Version": "25.0.0"}}
        return DockerResult(stdout=json.dumps(version_data), stderr="", exit_code=0)


async def test_service_implements_sandbox_protocol(tmp_path: Path) -> None:
    service = SandboxService(FakeRunner())
    # Static protocol check:
    sandbox: Sandbox = service
    assert {"docker", "environment", "rebuild", "run", "remove_task", "close"} <= set(dir(sandbox))

    status = await service.docker()
    assert status == DockerStatus(available=True, version="25.0.0", detail=None)

    env = await service.environment("prj_123")
    assert env == EnvironmentStatus(state="none", tag=None, detail=None)

    with pytest.raises(DomainError):
        await service.rebuild("prj_123")

    task = TaskRef(
        id="tsk_1",
        title="Test task",
        branch="agent/task-1",
        worktree=tmp_path / "worktree",
        status="active",
    )
    res = await service.run("prj_123", task, "echo hi", 60)
    assert isinstance(res, CommandResult)
    assert res.exit_code == 0

    # remove_task and close are safe no-ops
    await service.remove_task("prj_123", "tsk_1")
    await service.close()
