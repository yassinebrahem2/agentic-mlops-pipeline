from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import cast

import pytest

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.config import APP_NAME
from mlops_agents.connections.contract import LOCAL_FOLDER_KIND
from mlops_agents.connections.kinds.local_folder import LocalFolderKind
from mlops_agents.connections.registry import KindRegistry
from mlops_agents.connections.service import ConnectionsService
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.paths import DataPaths
from mlops_agents.sandbox.containers import mounts_fingerprint
from mlops_agents.sandbox.contract import SandboxUnavailable
from mlops_agents.sandbox.docker import DockerResult
from mlops_agents.sandbox.service import SandboxService
from mlops_agents.storage.databases import Databases
from tests.sandbox.fakes import FakeDockerRunner, FakeEventLog, MemorySandboxProjects


async def test_service_remove_task_and_close(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    service = SandboxService(
        runner=runner, projects=MemorySandboxProjects({"prj_1": worktree}), events=FakeEventLog()
    )

    task = TaskRef(
        id="task_svc_1",
        title="Task Svc",
        branch="branch-1",
        worktree=worktree,
        status="active",
    )

    cname = await service.ensure_container("prj_1", task)
    assert cname == f"{APP_NAME}-task_svc_1"
    assert cname in runner.containers

    # Remove task
    await service.remove_task("prj_1", "task_svc_1")
    assert cname not in runner.containers

    # Ensure container again
    await service.ensure_container("prj_1", task)
    assert cname in runner.containers

    # Close cleans up all containers
    await service.close()
    assert cname not in runner.containers


async def test_service_network_settings_and_container_recreation(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    service = SandboxService(
        runner=runner, projects=MemorySandboxProjects({"prj_1": worktree}), events=FakeEventLog()
    )

    task = TaskRef(
        id="task_net_1",
        title="Task Net",
        branch="branch-1",
        worktree=worktree,
        status="active",
    )

    # Default network is False
    assert await service.get_network("prj_1") is False

    # Start container with default network=False
    cname = await service.ensure_container("prj_1", task)
    c1_host = cast(dict[str, object], runner.containers[cname]["HostConfig"])
    assert c1_host["NetworkMode"] == "none"

    # Update network setting to True
    res = await service.set_network("prj_1", True)
    assert res is True
    assert await service.get_network("prj_1") is True

    # Changing network setting stops and removes running containers for prj_1
    assert cname not in runner.containers

    # Calling ensure_container again starts it with network enabled
    cname2 = await service.ensure_container("prj_1", task)
    assert cname2 == cname
    c2_host = cast(dict[str, object], runner.containers[cname]["HostConfig"])
    assert c2_host["NetworkMode"] == "default"


async def test_service_cleanup_orphans(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    # Pre-populate an orphan container
    runner.containers[f"{APP_NAME}-orphan"] = {
        "Id": "cid_orphan",
        "Name": f"/{APP_NAME}-orphan",
        "State": {"Running": True, "Status": "running"},
        "Config": {"Image": "some_img", "Labels": {f"{APP_NAME}": "true"}},
    }

    service = SandboxService(runner=runner, projects=MemorySandboxProjects(), events=FakeEventLog())
    await service.cleanup_orphans()
    assert f"{APP_NAME}-orphan" not in runner.containers


@pytest.mark.parametrize("operation", ["inspect", "rm", "run"])
@pytest.mark.parametrize("error", [TimeoutError("operation timed out"), OSError("I/O failed")])
async def test_container_operational_failure_becomes_unavailable(
    tmp_path: Path,
    operation: str,
    error: OSError,
) -> None:
    class FailedOperation(FakeDockerRunner):
        async def run(
            self,
            args: Sequence[str],
            *,
            timeout_s: float | None = None,
            secrets: Iterable[str] = (),
        ) -> DockerResult:
            if args[0] == operation:
                raise error
            return await super().run(args, timeout_s=timeout_s, secrets=secrets)

    service = SandboxService(
        FailedOperation(), projects=MemorySandboxProjects(), events=FakeEventLog()
    )
    task = TaskRef("tsk_container_failure", "Failure", "branch", tmp_path, "active")
    with pytest.raises(SandboxUnavailable):
        await service.ensure_container("prj_1", task)


async def test_unexpected_container_failure_surfaces(tmp_path: Path) -> None:
    class BrokenRunner(FakeDockerRunner):
        async def run(
            self,
            args: Sequence[str],
            *,
            timeout_s: float | None = None,
            secrets: Iterable[str] = (),
        ) -> DockerResult:
            if args[0] == "inspect":
                raise RuntimeError("runner defect")
            return await super().run(args, timeout_s=timeout_s, secrets=secrets)

    service = SandboxService(
        BrokenRunner(), projects=MemorySandboxProjects(), events=FakeEventLog()
    )
    task = TaskRef("tsk_unexpected", "Failure", "branch", tmp_path, "active")
    with pytest.raises(RuntimeError, match="runner defect"):
        await service.ensure_container("prj_1", task)


async def test_ensure_container_failed_build_raises_unavailable_with_detail(tmp_path: Path) -> None:
    class FailingBuildDocker(FakeDockerRunner):
        async def run(
            self,
            args: Sequence[str],
            *,
            timeout_s: float | None = None,
            secrets: Iterable[str] = (),
        ) -> DockerResult:
            if args[0] == "build":
                return DockerResult(
                    stdout="",
                    stderr="ERROR: failed to solve: syntax error at line 5",
                    exit_code=1,
                )
            return await super().run(args, timeout_s=timeout_s, secrets=secrets)

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("badpkg\n", encoding="utf-8")
    service = SandboxService(
        runner=FailingBuildDocker(),
        projects=MemorySandboxProjects({"prj_1": worktree}),
        events=FakeEventLog(),
    )
    task = TaskRef("tsk_build_fail", "Build Fail", "branch", worktree, "active")

    with pytest.raises(SandboxUnavailable) as exc_info:
        await service.ensure_container("prj_1", task)
    assert "syntax error at line 5" in str(exc_info.value)


async def test_service_paused_context_manager(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    service = SandboxService(
        runner=runner, projects=MemorySandboxProjects({"prj_1": worktree}), events=FakeEventLog()
    )
    task = TaskRef("tsk_pause_1", "Task Pause", "branch", worktree, "active")

    # 1. When container does not exist, paused is a clean no-op
    calls_before = len(runner.calls)
    async with service.paused("prj_1", task.id):
        pass
    assert not any(c[0] in ("pause", "unpause") for c in runner.calls[calls_before:])

    # 2. When container exists and is running
    cname = await service.ensure_container("prj_1", task)
    assert cname in runner.containers

    async with service.paused("prj_1", task.id):
        c_state = cast(dict[str, object], runner.containers[cname]["State"])
        assert c_state.get("Paused") is True
        assert any(c[0] == "pause" for c in runner.calls)

    # After exiting context, container is unpaused
    c_state_after = cast(dict[str, object], runner.containers[cname]["State"])
    assert c_state_after.get("Paused") is False
    assert any(c[0] == "unpause" for c in runner.calls)


async def test_service_task_lock_serializes_paused(tmp_path: Path) -> None:
    import asyncio

    runner = FakeDockerRunner()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    service = SandboxService(
        runner=runner, projects=MemorySandboxProjects({"prj_1": worktree}), events=FakeEventLog()
    )
    task = TaskRef("tsk_lock_1", "Lock Task", "branch", worktree, "active")
    await service.ensure_container("prj_1", task)

    events_sequence: list[str] = []
    first_entered = asyncio.Event()
    second_waiting = asyncio.Event()

    async def pause_block_1() -> None:
        async with service.paused("prj_1", task.id):
            events_sequence.append("p1_started")
            first_entered.set()
            await second_waiting.wait()
            events_sequence.append("p1_ending")

    async def pause_block_2() -> None:
        await first_entered.wait()
        events_sequence.append("p2_waiting")
        second_waiting.set()
        async with service.paused("prj_1", task.id):
            events_sequence.append("p2_started")

    await asyncio.gather(pause_block_1(), pause_block_2())
    assert events_sequence == [
        "p1_started",
        "p2_waiting",
        "p1_ending",
        "p2_started",
    ]


async def test_service_connection_lifecycle_recreates_container(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    paths = DataPaths(home)
    paths.ensure()

    databases = Databases(paths)
    await databases.app()
    project_id = "prj_conlifecycle"
    await databases.project(project_id)

    events = SqliteEventLog(databases)
    registry = KindRegistry([LocalFolderKind(paths.root)])
    conns_service = ConnectionsService(databases, events, registry)

    runner = FakeDockerRunner()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    projects = MemorySandboxProjects(
        roots={project_id: worktree},
        connections=conns_service,
    )
    service = SandboxService(runner=runner, projects=projects, events=events)
    task = TaskRef("tsk_con_1", "Con Task", "branch", worktree, "active")

    try:
        # 1. No connections: container starts with empty fingerprint
        cname = await service.ensure_container(project_id, task)
        info1 = runner.containers[cname]
        labels1 = cast(dict[str, str], cast(dict[str, object], info1["Config"])["Labels"])
        assert labels1[f"{APP_NAME}.mounts"] == mounts_fingerprint([])

        # 2. Add connection
        data_a = tmp_path / "data_a"
        data_a.mkdir()
        (data_a / "a.csv").write_text("col1\nval1", encoding="utf-8")
        con_a = await conns_service.create(
            project_id, LOCAL_FOLDER_KIND, "data-a", {"path": str(data_a)}
        )
        mounts_a = await conns_service.mounts(project_id)
        assert len(mounts_a) == 1

        # Calling ensure_container recreates container because mounts fingerprint changed
        calls_before = len(runner.calls)
        cname2 = await service.ensure_container(project_id, task)
        assert cname2 == cname
        assert any(c[0] == "rm" for c in runner.calls[calls_before:])
        info2 = runner.containers[cname]
        labels2 = cast(dict[str, str], cast(dict[str, object], info2["Config"])["Labels"])
        assert labels2[f"{APP_NAME}.mounts"] == mounts_fingerprint(mounts_a)

        # Calling ensure_container again with same connection reuses it
        calls_reuse = len(runner.calls)
        await service.ensure_container(project_id, task)
        assert not any(c[0] in ("rm", "run") for c in runner.calls[calls_reuse:])

        # 3. Update connection data_a to data_a_new
        data_a_new = tmp_path / "data_a_new"
        data_a_new.mkdir()
        (data_a_new / "a.csv").write_text("col1\nval2", encoding="utf-8")
        await conns_service.update(project_id, con_a.id, {"path": str(data_a_new)})
        mounts_updated = await conns_service.mounts(project_id)

        calls_update = len(runner.calls)
        await service.ensure_container(project_id, task)
        assert any(c[0] == "rm" for c in runner.calls[calls_update:])
        info3 = runner.containers[cname]
        labels3 = cast(dict[str, str], cast(dict[str, object], info3["Config"])["Labels"])
        assert labels3[f"{APP_NAME}.mounts"] == mounts_fingerprint(mounts_updated)

        # 4. Add second connection data-b
        data_b = tmp_path / "data_b"
        data_b.mkdir()
        con_b = await conns_service.create(
            project_id, LOCAL_FOLDER_KIND, "data-b", {"path": str(data_b)}
        )
        mounts_two = await conns_service.mounts(project_id)
        assert len(mounts_two) == 2

        calls_add_b = len(runner.calls)
        await service.ensure_container(project_id, task)
        assert any(c[0] == "rm" for c in runner.calls[calls_add_b:])
        info4 = runner.containers[cname]
        labels4 = cast(dict[str, str], cast(dict[str, object], info4["Config"])["Labels"])
        assert labels4[f"{APP_NAME}.mounts"] == mounts_fingerprint(mounts_two)

        # 5. Delete connection data-b
        await conns_service.delete(project_id, con_b.id)
        mounts_after_del = await conns_service.mounts(project_id)
        assert len(mounts_after_del) == 1

        calls_del_b = len(runner.calls)
        await service.ensure_container(project_id, task)
        assert any(c[0] == "rm" for c in runner.calls[calls_del_b:])
        info5 = runner.containers[cname]
        labels5 = cast(dict[str, str], cast(dict[str, object], info5["Config"])["Labels"])
        assert labels5[f"{APP_NAME}.mounts"] == mounts_fingerprint(mounts_after_del)
    finally:
        await databases.dispose()
