from __future__ import annotations

import json
from pathlib import Path

import pytest

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.sandbox.docker import CliDockerRunner
from mlops_agents.sandbox.service import SandboxService
from tests.sandbox.fakes import FakeEventLog, MemorySandboxProjects


@pytest.mark.docker
async def test_real_docker_paused_lifecycle(tmp_path: Path) -> None:
    runner = CliDockerRunner()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("six\n", encoding="utf-8")
    service = SandboxService(
        runner=runner,
        projects=MemorySandboxProjects({"prj_paused": worktree}),
        events=FakeEventLog(),
    )
    task = TaskRef("realpause1", "Real Pause", "branch", worktree, "active")
    cname = await service.ensure_container("prj_paused", task)
    try:
        # Before paused: running and not paused
        inspect_res = await runner.run(["inspect", cname])
        assert inspect_res.exit_code == 0
        info = json.loads(inspect_res.stdout)[0]
        assert info["State"]["Running"] is True
        assert info["State"]["Paused"] is False

        async with service.paused("prj_paused", task.id):
            inspect_paused = await runner.run(["inspect", cname])
            assert inspect_paused.exit_code == 0
            info_paused = json.loads(inspect_paused.stdout)[0]
            assert info_paused["State"]["Paused"] is True

        # After paused block: unpaused
        inspect_after = await runner.run(["inspect", cname])
        assert inspect_after.exit_code == 0
        info_after = json.loads(inspect_after.stdout)[0]
        assert info_after["State"]["Paused"] is False
    finally:
        await service.remove_task("prj_paused", task.id)
