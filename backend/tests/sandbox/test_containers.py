from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import cast

import pytest

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.config import APP_NAME
from mlops_agents.sandbox.containers import ContainerManager
from mlops_agents.sandbox.contract import SandboxUnavailable
from mlops_agents.sandbox.docker import CliDockerRunner, DockerResult, DockerRunner


class FakeContainersDocker(DockerRunner):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        # container_name -> dict representing inspect JSON item
        self.containers: dict[str, dict[str, object]] = {}
        self.fail_run: bool = False

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None = None,
        secrets: Iterable[str] = (),
    ) -> DockerResult:
        cmd = list(args)
        self.calls.append(cmd)

        if cmd[0] == "inspect":
            name = cmd[1]
            if name in self.containers:
                return DockerResult(
                    stdout=json.dumps([self.containers[name]]), stderr="", exit_code=0
                )
            return DockerResult(stdout="", stderr=f"Error: No such container: {name}", exit_code=1)

        if cmd[0] == "run":
            if self.fail_run:
                return DockerResult(stdout="", stderr="docker run failed", exit_code=1)
            name_idx = cmd.index("--name") + 1
            name = cmd[name_idx]
            # parse labels
            labels: dict[str, str] = {}
            for i, arg in enumerate(cmd):
                if arg == "--label" and i + 1 < len(cmd):
                    k, v = cmd[i + 1].split("=", 1)
                    labels[k] = v

            net_mode = (
                "none"
                if "--network" in cmd and cmd[cmd.index("--network") + 1] == "none"
                else "default"
            )
            # find image tag (argument before sleep)
            sleep_idx = cmd.index("sleep")
            image_tag = cmd[sleep_idx - 1]

            self.containers[name] = {
                "Id": f"cid_{name}",
                "Name": f"/{name}",
                "State": {"Running": True, "Status": "running"},
                "Config": {
                    "Image": image_tag,
                    "Labels": labels,
                },
                "HostConfig": {
                    "NetworkMode": net_mode,
                },
            }
            return DockerResult(stdout=f"cid_{name}", stderr="", exit_code=0)

        if cmd[0] == "rm":
            for target in cmd[2:]:
                self.containers.pop(target, None)
            return DockerResult(stdout="Deleted", stderr="", exit_code=0)

        if cmd[0] == "ps":
            matched_ids: list[str] = []
            filter_arg = next((cmd[i + 1] for i, a in enumerate(cmd) if a == "--filter"), None)
            for name, cinfo in self.containers.items():
                if filter_arg:
                    if filter_arg == f"label={APP_NAME}=true":
                        matched_ids.append(name)
                    elif filter_arg.startswith(f"label={APP_NAME}.project="):
                        expected_proj = filter_arg.split("=", 2)[2]
                        cfg_raw = cinfo.get("Config")
                        if isinstance(cfg_raw, dict):
                            cfg = cast(dict[str, object], cfg_raw)
                            raw_lbls = cfg.get("Labels")
                            if isinstance(raw_lbls, dict):
                                lbls = cast(dict[str, str], raw_lbls)
                                if lbls.get(f"{APP_NAME}.project") == expected_proj:
                                    matched_ids.append(name)
                else:
                    matched_ids.append(name)
            return DockerResult(stdout="\n".join(matched_ids), stderr="", exit_code=0)

        return DockerResult(stdout="", stderr="", exit_code=0)


async def test_container_manager_inspect_and_start(tmp_path: Path) -> None:
    runner = FakeContainersDocker()
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
    class BadInspectDocker(FakeContainersDocker):
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
    runner = FakeContainersDocker()
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

    with pytest.raises(SandboxUnavailable, match="Failed to start sandbox container"):
        await manager.start("prj_1", task, "mlops-agents-env:prj_1-abc")


async def test_ensure_container_reuses_running(tmp_path: Path) -> None:
    runner = FakeContainersDocker()
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
    runner = FakeContainersDocker()
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
    runner = FakeContainersDocker()
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


async def test_ensure_container_recreates_stopped_container(tmp_path: Path) -> None:
    runner = FakeContainersDocker()
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
    runner = FakeContainersDocker()
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
    runner = FakeContainersDocker()
    manager = ContainerManager(runner)
    worktree = tmp_path / "worktree"
    task1 = TaskRef(id="t1", title="T1", branch="b1", worktree=worktree, status="active")
    task2 = TaskRef(id="t2", title="T2", branch="b2", worktree=worktree, status="active")

    await manager.start("prj_1", task1, "mlops-agents-env:prj_1-tag")
    await manager.start("prj_2", task2, "mlops-agents-env:prj_2-tag")

    await manager.cleanup_all()
    assert await manager.inspect(f"{APP_NAME}-t1") is None
    assert await manager.inspect(f"{APP_NAME}-t2") is None


class FailingDockerRunner(DockerRunner):
    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None = None,
        secrets: Iterable[str] = (),
    ) -> DockerResult:
        raise FileNotFoundError("Docker not found")


async def test_cleanup_orphans_docker_not_available() -> None:
    manager = ContainerManager(FailingDockerRunner())
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
