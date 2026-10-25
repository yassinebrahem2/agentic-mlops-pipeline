from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import cast

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.config import APP_NAME
from mlops_agents.sandbox.docker import DockerResult, DockerRunner
from mlops_agents.sandbox.service import SandboxService


class FakeServiceDocker(DockerRunner):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.containers: dict[str, dict[str, object]] = {}
        self.images: set[str] = set()

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None = None,
        secrets: Iterable[str] = (),
    ) -> DockerResult:
        cmd = list(args)
        self.calls.append(cmd)

        if cmd[0] == "version":
            data = {"Client": {"Version": "27.0.0"}, "Server": {"Version": "27.0.0"}}
            return DockerResult(stdout=json.dumps(data), stderr="", exit_code=0)

        if cmd[0] == "image" and cmd[1] == "inspect":
            tag = cmd[2]
            if tag in self.images:
                return DockerResult(stdout="{}", stderr="", exit_code=0)
            return DockerResult(stdout="", stderr="no such image", exit_code=1)

        if cmd[0] == "build":
            tag_idx = cmd.index("-t") + 1
            tag = cmd[tag_idx]
            self.images.add(tag)
            return DockerResult(stdout="Successfully built", stderr="", exit_code=0)

        if cmd[0] == "inspect":
            name = cmd[1]
            if name in self.containers:
                return DockerResult(
                    stdout=json.dumps([self.containers[name]]), stderr="", exit_code=0
                )
            return DockerResult(stdout="", stderr=f"Error: No such container: {name}", exit_code=1)

        if cmd[0] == "run":
            name_idx = cmd.index("--name") + 1
            name = cmd[name_idx]
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
            matched: list[str] = []
            filter_arg = next((cmd[i + 1] for i, a in enumerate(cmd) if a == "--filter"), None)
            for name, cinfo in self.containers.items():
                if filter_arg:
                    if filter_arg == f"label={APP_NAME}=true":
                        matched.append(name)
                    elif filter_arg.startswith(f"label={APP_NAME}.project="):
                        expected_proj = filter_arg.split("=", 2)[2]
                        cfg_raw = cinfo.get("Config")
                        if isinstance(cfg_raw, dict):
                            cfg = cast(dict[str, object], cfg_raw)
                            raw_lbls = cfg.get("Labels")
                            if isinstance(raw_lbls, dict):
                                lbls = cast(dict[str, str], raw_lbls)
                                if lbls.get(f"{APP_NAME}.project") == expected_proj:
                                    matched.append(name)
                else:
                    matched.append(name)
            return DockerResult(stdout="\n".join(matched), stderr="", exit_code=0)

        return DockerResult(stdout="", stderr="", exit_code=0)


async def test_service_remove_task_and_close(tmp_path: Path) -> None:
    runner = FakeServiceDocker()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    service = SandboxService(runner=runner, repo_roots={"prj_1": worktree})

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
    runner = FakeServiceDocker()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    service = SandboxService(runner=runner, repo_roots={"prj_1": worktree})

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
    runner = FakeServiceDocker()
    # Pre-populate an orphan container
    runner.containers[f"{APP_NAME}-orphan"] = {
        "Id": "cid_orphan",
        "Name": f"/{APP_NAME}-orphan",
        "State": {"Running": True, "Status": "running"},
        "Config": {"Image": "some_img", "Labels": {f"{APP_NAME}": "true"}},
    }

    service = SandboxService(runner=runner)
    await service.cleanup_orphans()
    assert f"{APP_NAME}-orphan" not in runner.containers
