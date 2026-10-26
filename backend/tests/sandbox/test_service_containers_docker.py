from __future__ import annotations

import json
from pathlib import Path

import pytest

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.sandbox.containers import ContainerManager
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


@pytest.mark.docker
async def test_real_docker_recovers_stale_pause(tmp_path: Path) -> None:
    runner = CliDockerRunner()
    service = SandboxService(
        runner, projects=MemorySandboxProjects({"prj_recovery": tmp_path}), events=FakeEventLog()
    )
    task = TaskRef("tsk_pause_recovery", "Recovery", "branch", tmp_path, "active")
    name = await service.ensure_container("prj_recovery", task)
    manager = ContainerManager(runner)
    try:
        await manager.pause(name)
        assert await service.ensure_container("prj_recovery", task) == name
        result = await runner.run(["exec", name, "bash", "-lc", "echo recovered"], timeout_s=10)
        assert result.exit_code == 0 and result.stdout.strip() == "recovered"
        await manager.pause(name)
        async with service.paused("prj_recovery", task.id):
            info = await manager.inspect(name)
            assert info is not None and info.paused
        info = await manager.inspect(name)
        assert info is not None and not info.paused
        await manager.pause(name)
        await service.cleanup_orphans()
        assert await manager.inspect(name) is None
    finally:
        await service.remove_task("prj_recovery", task.id)


@pytest.mark.docker
async def test_real_docker_paused_container_removal_paths(tmp_path: Path) -> None:
    runner = CliDockerRunner()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("six\n", encoding="utf-8")
    service = SandboxService(
        runner=runner,
        projects=MemorySandboxProjects({"prj_rm_paused": worktree}),
        events=FakeEventLog(),
    )
    task = TaskRef("tsk_rm_paused", "RM Paused", "branch", worktree, "active")
    manager = ContainerManager(runner)

    # 1. ensure_container with mismatching image removes paused container
    cname = await service.ensure_container("prj_rm_paused", task)
    try:
        await manager.pause(cname)
        info = await manager.inspect(cname)
        assert info is not None and info.paused
        raw1 = await runner.run(["inspect", cname])
        cid1 = json.loads(raw1.stdout)[0]["Id"]
        tag1 = info.image_tag

        # Change requirements to trigger a new tag
        (worktree / "requirements.txt").write_text("six\n# changed\n", encoding="utf-8")
        new_cname = await service.ensure_container("prj_rm_paused", task)
        assert new_cname == cname
        new_info = await manager.inspect(cname)
        assert new_info is not None and new_info.running and not new_info.paused
        raw2 = await runner.run(["inspect", cname])
        cid2 = json.loads(raw2.stdout)[0]["Id"]
        assert cid2 != cid1
        assert new_info.image_tag != tag1

        # 2. start() removes paused container
        await manager.pause(cname)
        info = await manager.inspect(cname)
        assert info is not None and info.paused
        started_name = await manager.start(
            "prj_rm_paused", task, new_info.image_tag or "python:3.12-slim"
        )
        assert started_name == cname
        started_info = await manager.inspect(cname)
        assert started_info is not None and started_info.running and not started_info.paused
        raw3 = await runner.run(["inspect", cname])
        cid3 = json.loads(raw3.stdout)[0]["Id"]
        assert cid3 != cid2

        # 3. remove_task() removes paused container
        await manager.pause(cname)
        info = await manager.inspect(cname)
        assert info is not None and info.paused
        await service.remove_task("prj_rm_paused", task.id)
        assert await manager.inspect(cname) is None
    finally:
        await manager.remove(cname)
