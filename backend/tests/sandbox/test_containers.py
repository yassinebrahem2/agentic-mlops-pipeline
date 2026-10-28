from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.config import APP_NAME
from mlops_agents.sandbox.containers import ContainerManager, mounts_fingerprint
from mlops_agents.sandbox.contract import SandboxUnavailable
from mlops_agents.sandbox.docker import CliDockerRunner, DockerResult
from mlops_agents.sandbox.projects import SandboxMount as Mount
from tests.sandbox.fakes import FakeDockerRunner


async def test_container_manager_inspect_and_start(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    manager = ContainerManager(runner)
    worktree = tmp_path / "worktree"
    task = TaskRef(
        id="task_1",
        title="Task 1",
        branch="branch-1",
        worktree=worktree,
        status="active",
    )

    # Inspect missing container returns None
    assert await manager.inspect(f"{APP_NAME}-task_1") is None

    # Start container
    cname = await manager.start("prj_1", task, "mlops-agents-env:prj_1-abc", network=False)
    assert cname == f"{APP_NAME}-task_1"

    # Inspect returns valid info
    info = await manager.inspect(cname)
    assert info is not None
    assert info.name == cname
    assert info.running is True
    assert info.image_tag == "mlops-agents-env:prj_1-abc"
    assert info.network is False
    assert info.project_id == "prj_1"
    assert info.task_id == "task_1"


async def test_container_manager_inspect_invalid_json_raises_unavailable() -> None:
    class BadInspectDocker(FakeDockerRunner):
        async def run(
            self,
            args: Sequence[str],
            *,
            timeout_s: float | None = None,
            secrets: Iterable[str] = (),
        ) -> DockerResult:
            if args[0] == "inspect":
                return DockerResult(stdout="not valid json", stderr="", exit_code=0)
            return await super().run(args, timeout_s=timeout_s, secrets=secrets)

    manager = ContainerManager(BadInspectDocker())
    with pytest.raises(SandboxUnavailable, match="Failed to parse inspect output"):
        await manager.inspect("some_container")


async def test_container_manager_start_failure(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    runner.fail_run = True
    manager = ContainerManager(runner)
    worktree = tmp_path / "worktree"
    task = TaskRef(
        id="task_fail",
        title="Task Fail",
        branch="branch-fail",
        worktree=worktree,
        status="active",
    )

    with pytest.raises(SandboxUnavailable, match="Failed to start sandbox container:"):
        await manager.start("prj_1", task, "mlops-agents-env:prj_1-abc")


async def test_container_manager_start_failure_with_mounts_names_paths(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    runner.fail_run = True
    manager = ContainerManager(runner)
    worktree = tmp_path / "worktree"
    task = TaskRef(
        id="task_fail_mounts",
        title="Task Fail Mounts",
        branch="branch-fail",
        worktree=worktree,
        status="active",
    )
    mounts = [
        Mount(host_path=Path("/host/events"), container_path="/data/events"),
        Mount(host_path=Path("/host/hotels"), container_path="/data/hotel-data"),
    ]

    with pytest.raises(
        SandboxUnavailable,
        match=r"while mounting /data/events, /data/hotel-data: docker run failed",
    ):
        await manager.start("prj_1", task, "mlops-agents-env:prj_1-abc", mounts=mounts)


async def test_container_manager_inspect_mounts_label(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    manager = ContainerManager(runner)
    worktree = tmp_path / "worktree"
    task = TaskRef(
        id="task_inspect_m",
        title="Task Inspect Mounts",
        branch="branch-m",
        worktree=worktree,
        status="active",
    )
    mounts = [Mount(host_path=Path("/host/hotels"), container_path="/data/hotel-data")]
    cname = await manager.start("prj_1", task, "mlops-agents-env:prj_1-abc", mounts=mounts)
    info = await manager.inspect(cname)
    assert info is not None
    assert info.mounts == mounts_fingerprint(mounts)


async def test_ensure_container_reuses_running(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    manager = ContainerManager(runner)
    worktree = tmp_path / "worktree"
    task = TaskRef(
        id="task_reuse",
        title="Task Reuse",
        branch="branch-reuse",
        worktree=worktree,
        status="active",
    )

    # First call starts the container
    name1 = await manager.ensure_container(
        "prj_1", task, "mlops-agents-env:prj_1-abc", network=False
    )
    call_count_after_first = len(runner.calls)

    # Second call reuses without executing docker run again
    name2 = await manager.ensure_container(
        "prj_1", task, "mlops-agents-env:prj_1-abc", network=False
    )
    assert name1 == name2
    # Only inspect was called on second call
    assert not any(c[0] == "run" for c in runner.calls[call_count_after_first:])


async def test_ensure_container_recreates_on_tag_change(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    manager = ContainerManager(runner)
    worktree = tmp_path / "worktree"
    task = TaskRef(
        id="task_recreate_tag",
        title="Task Recreate Tag",
        branch="branch-tag",
        worktree=worktree,
        status="active",
    )

    await manager.ensure_container("prj_1", task, "mlops-agents-env:prj_1-tag1", network=False)
    info1 = await manager.inspect(f"{APP_NAME}-task_recreate_tag")
    assert info1 is not None
    assert info1.image_tag == "mlops-agents-env:prj_1-tag1"

    # Image tag changes
    await manager.ensure_container("prj_1", task, "mlops-agents-env:prj_1-tag2", network=False)
    info2 = await manager.inspect(f"{APP_NAME}-task_recreate_tag")
    assert info2 is not None
    assert info2.image_tag == "mlops-agents-env:prj_1-tag2"

    # Verify rm was called to remove old container
    assert any(c[0] == "rm" for c in runner.calls)


async def test_ensure_container_recreates_on_network_change(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    manager = ContainerManager(runner)
    worktree = tmp_path / "worktree"
    task = TaskRef(
        id="task_recreate_net",
        title="Task Recreate Net",
        branch="branch-net",
        worktree=worktree,
        status="active",
    )

    await manager.ensure_container("prj_1", task, "mlops-agents-env:prj_1-abc", network=False)
    info1 = await manager.inspect(f"{APP_NAME}-task_recreate_net")
    assert info1 is not None
    assert info1.network is False

    # Network setting changes to True
    await manager.ensure_container("prj_1", task, "mlops-agents-env:prj_1-abc", network=True)
    info2 = await manager.inspect(f"{APP_NAME}-task_recreate_net")
    assert info2 is not None
    assert info2.network is True


async def test_ensure_container_reuses_matching_mounts(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    manager = ContainerManager(runner)
    worktree = tmp_path / "worktree"
    task = TaskRef(
        id="task_mount_reuse",
        title="Task Mount Reuse",
        branch="branch-mr",
        worktree=worktree,
        status="active",
    )
    mounts = [Mount(host_path=Path("/host/d1"), container_path="/data/d1")]

    name1 = await manager.ensure_container(
        "prj_1", task, "mlops-agents-env:prj_1-abc", network=False, mounts=mounts
    )
    calls_after_first = len(runner.calls)

    name2 = await manager.ensure_container(
        "prj_1", task, "mlops-agents-env:prj_1-abc", network=False, mounts=mounts
    )
    assert name1 == name2
    # Second call reuses running container (only inspect called, no rm, no run)
    assert not any(c[0] in ("rm", "run") for c in runner.calls[calls_after_first:])


async def test_ensure_container_recreates_on_mounts_change(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    manager = ContainerManager(runner)
    worktree = tmp_path / "worktree"
    task = TaskRef(
        id="task_mount_change",
        title="Task Mount Change",
        branch="branch-mc",
        worktree=worktree,
        status="active",
    )
    mounts1 = [Mount(host_path=Path("/host/d1"), container_path="/data/d1")]
    mounts2 = [
        Mount(host_path=Path("/host/d1"), container_path="/data/d1"),
        Mount(host_path=Path("/host/d2"), container_path="/data/d2"),
    ]

    await manager.ensure_container(
        "prj_1", task, "mlops-agents-env:prj_1-abc", network=False, mounts=mounts1
    )
    info1 = await manager.inspect(f"{APP_NAME}-task_mount_change")
    assert info1 is not None
    assert info1.mounts == mounts_fingerprint(mounts1)

    calls_before = len(runner.calls)
    await manager.ensure_container(
        "prj_1", task, "mlops-agents-env:prj_1-abc", network=False, mounts=mounts2
    )
    info2 = await manager.inspect(f"{APP_NAME}-task_mount_change")
    assert info2 is not None
    assert info2.mounts == mounts_fingerprint(mounts2)

    # Old container was removed and new one started
    assert any(c[0] == "rm" for c in runner.calls[calls_before:])
    assert any(c[0] == "run" for c in runner.calls[calls_before:])


async def test_ensure_container_recreates_when_missing_mounts_label(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    manager = ContainerManager(runner)
    worktree = tmp_path / "worktree"
    task = TaskRef(
        id="task_no_label",
        title="Task No Label",
        branch="branch-nl",
        worktree=worktree,
        status="active",
    )

    cname = f"{APP_NAME}-task_no_label"
    # Pre-populate container with no mounts label
    runner.containers[cname] = {
        "Id": "cid_no_label",
        "Name": f"/{cname}",
        "State": {"Running": True, "Paused": False, "Status": "running"},
        "Config": {
            "Labels": {
                f"{APP_NAME}": "true",
                f"{APP_NAME}.project": "prj_1",
                f"{APP_NAME}.task": task.id,
                f"{APP_NAME}.image_tag": "mlops-agents-env:prj_1-abc",
                f"{APP_NAME}.network": "false",
            },
            "Image": "mlops-agents-env:prj_1-abc",
        },
        "HostConfig": {"NetworkMode": "none"},
    }

    info = await manager.inspect(cname)
    assert info is not None
    assert info.mounts is None

    # Calling ensure_container should remove and recreate it because mounts label is missing
    await manager.ensure_container(
        "prj_1", task, "mlops-agents-env:prj_1-abc", network=False, mounts=[]
    )
    assert any(c[0] == "rm" for c in runner.calls)
    info_after = await manager.inspect(cname)
    assert info_after is not None
    assert info_after.mounts == mounts_fingerprint([])


async def test_ensure_container_recreates_stopped_container(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    manager = ContainerManager(runner)
    worktree = tmp_path / "worktree"
    task = TaskRef(
        id="task_stopped",
        title="Task Stopped",
        branch="branch-stopped",
        worktree=worktree,
        status="active",
    )

    await manager.ensure_container("prj_1", task, "mlops-agents-env:prj_1-abc", network=False)
    # Simulate container stopped
    cname = f"{APP_NAME}-task_stopped"
    state = runner.containers[cname]["State"]
    if isinstance(state, dict):
        state["Running"] = False

    # Ensure container detects it stopped and recreates it
    await manager.ensure_container("prj_1", task, "mlops-agents-env:prj_1-abc", network=False)
    info = await manager.inspect(cname)
    assert info is not None
    assert info.running is True


async def test_remove_container_and_remove_project(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    manager = ContainerManager(runner)
    worktree = tmp_path / "worktree"
    task1 = TaskRef(id="t1", title="T1", branch="b1", worktree=worktree, status="active")
    task2 = TaskRef(id="t2", title="T2", branch="b2", worktree=worktree, status="active")
    task_other = TaskRef(id="to", title="TO", branch="bo", worktree=worktree, status="active")

    await manager.start("prj_1", task1, "mlops-agents-env:prj_1-tag")
    await manager.start("prj_1", task2, "mlops-agents-env:prj_1-tag")
    await manager.start("prj_other", task_other, "mlops-agents-env:prj_other-tag")

    # Remove single container
    await manager.remove_task("prj_1", "t1")
    assert await manager.inspect(f"{APP_NAME}-t1") is None
    assert await manager.inspect(f"{APP_NAME}-t2") is not None
    assert await manager.inspect(f"{APP_NAME}-to") is not None

    # Remove all for prj_1
    await manager.remove_project_containers("prj_1")
    assert await manager.inspect(f"{APP_NAME}-t2") is None
    assert await manager.inspect(f"{APP_NAME}-to") is not None


async def test_cleanup_all(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    manager = ContainerManager(runner)
    worktree = tmp_path / "worktree"
    task1 = TaskRef(id="t1", title="T1", branch="b1", worktree=worktree, status="active")
    task2 = TaskRef(id="t2", title="T2", branch="b2", worktree=worktree, status="active")

    await manager.start("prj_1", task1, "mlops-agents-env:prj_1-tag")
    await manager.start("prj_2", task2, "mlops-agents-env:prj_2-tag")

    await manager.cleanup_all()
    assert await manager.inspect(f"{APP_NAME}-t1") is None
    assert await manager.inspect(f"{APP_NAME}-t2") is None


async def test_cleanup_orphans_docker_not_available() -> None:
    manager = ContainerManager(FakeDockerRunner(not_installed=True))
    # Should not raise exception
    await manager.cleanup_orphans()
    # Should not raise exception
    await manager.cleanup_orphans()


@pytest.mark.docker
async def test_real_docker_container_lifecycle(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "hello.txt").write_text("hello from worktree", encoding="utf-8")

    runner = CliDockerRunner()
    manager = ContainerManager(runner)
    task = TaskRef(
        id="realdocker1",
        title="Real Docker Task",
        branch="branch-1",
        worktree=worktree,
        status="active",
    )
    image_tag = "python:3.12-slim"

    cname = await manager.ensure_container("prj_real", task, image_tag, network=False)
    try:
        info = await manager.inspect(cname)
        assert info is not None
        assert info.running is True
        assert info.network is False

        # Test command inside container sees worktree file
        res = await runner.run(["exec", cname, "cat", "/workspace/hello.txt"])
        assert res.exit_code == 0
        assert "hello from worktree" in res.stdout
    finally:
        await manager.remove_task("prj_real", "realdocker1")
        assert await manager.inspect(cname) is None
