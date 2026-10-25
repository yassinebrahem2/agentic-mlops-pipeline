"""Task container management for Docker sandbox."""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from typing import cast

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.config import APP_NAME
from mlops_agents.sandbox.contract import SandboxUnavailable
from mlops_agents.sandbox.docker import DockerRunner


def container_name(task_id: str) -> str:
    """Returns the container name for a task."""
    return f"{APP_NAME}-{task_id}"


def build_run_args(
    project_id: str,
    task: TaskRef,
    image_tag: str,
    *,
    network: bool = False,
    is_posix: bool | None = None,
) -> list[str]:
    """Builds docker run arguments for starting a task container."""
    posix = (os.name == "posix") if is_posix is None else is_posix
    name = container_name(task.id)
    args = [
        "run",
        "-d",
        "--name",
        name,
        "--label",
        f"{APP_NAME}=true",
        "--label",
        f"{APP_NAME}.project={project_id}",
        "--label",
        f"{APP_NAME}.task={task.id}",
        "--label",
        f"{APP_NAME}.image_tag={image_tag}",
        "--label",
        f"{APP_NAME}.network={'true' if network else 'false'}",
        "--cpus",
        "2",
        "--memory",
        "4g",
        "--pids-limit",
        "512",
        "--workdir",
        "/workspace",
        "-v",
        f"{task.worktree!s}:/workspace",
    ]
    if not network:
        args.extend(["--network", "none"])
    args.extend(
        [
            "-e",
            "HOME=/tmp",
            "-e",
            "PYTHONPATH=/workspace/src:/workspace",
        ]
    )
    if posix:
        uid = getattr(os, "getuid", lambda: 1000)()
        gid = getattr(os, "getgid", lambda: 1000)()
        args.extend(["-u", f"{uid}:{gid}"])
    args.append(image_tag)
    args.extend(["sleep", "infinity"])
    return args


@dataclass(frozen=True)
class ContainerInfo:
    name: str
    running: bool
    image_tag: str | None
    network: bool
    project_id: str | None
    task_id: str | None


class ContainerManager:
    """Manages Docker containers for tasks."""

    def __init__(self, runner: DockerRunner) -> None:
        self._runner = runner

    async def inspect(self, name: str) -> ContainerInfo | None:
        """Inspects a container and returns its info, or None if not found."""
        try:
            res = await self._runner.run(["inspect", name])
        except FileNotFoundError:
            return None
        if res.exit_code != 0:
            return None
        try:
            raw_data: object = json.loads(res.stdout)
            if not isinstance(raw_data, list) or not raw_data:
                return None
            first_raw = cast(list[object], raw_data)[0]
            if not isinstance(first_raw, dict):
                return None
            item = cast(dict[str, object], first_raw)
        except Exception:
            return None

        state_raw = item.get("State")
        state = cast(dict[str, object], state_raw) if isinstance(state_raw, dict) else {}
        running = bool(state.get("Running", False))

        config_raw = item.get("Config")
        config = cast(dict[str, object], config_raw) if isinstance(config_raw, dict) else {}
        labels: dict[str, str] = {}
        if isinstance(config.get("Labels"), dict):
            raw_labels = cast(dict[object, object], config["Labels"])
            labels = {str(k): str(v) for k, v in raw_labels.items()}
        raw_image = config.get("Image")
        image = raw_image if isinstance(raw_image, str) else None

        host_config_raw = item.get("HostConfig")
        host_config = (
            cast(dict[str, object], host_config_raw) if isinstance(host_config_raw, dict) else {}
        )
        raw_net = host_config.get("NetworkMode")
        net_mode = raw_net if isinstance(raw_net, str) else ""

        image_tag = labels.get(f"{APP_NAME}.image_tag") or image
        network_label = labels.get(f"{APP_NAME}.network")
        if network_label is not None:
            network = network_label.lower() == "true"
        else:
            network = net_mode != "none"

        project_id = labels.get(f"{APP_NAME}.project")
        task_id = labels.get(f"{APP_NAME}.task")

        return ContainerInfo(
            name=name,
            running=running,
            image_tag=image_tag,
            network=network,
            project_id=project_id,
            task_id=task_id,
        )

    async def start(
        self,
        project_id: str,
        task: TaskRef,
        image_tag: str,
        *,
        network: bool = False,
        is_posix: bool | None = None,
    ) -> str:
        """Starts a new container for the task and returns its name."""
        name = container_name(task.id)
        await self.remove(name)
        args = build_run_args(
            project_id,
            task,
            image_tag,
            network=network,
            is_posix=is_posix,
        )
        try:
            res = await self._runner.run(args)
        except FileNotFoundError as err:
            raise SandboxUnavailable("Docker is not installed or not on PATH.") from err
        if res.exit_code != 0:
            detail = res.stderr.strip() or res.stdout.strip() or "docker run failed"
            raise SandboxUnavailable(f"Failed to start sandbox container: {detail}")
        return name

    async def remove(self, name: str) -> None:
        """Removes a container by name if it exists."""
        with contextlib.suppress(Exception):
            await self._runner.run(["rm", "-f", name])

    async def remove_task(self, project_id: str, task_id: str) -> None:
        """Stops and removes the container for a task."""
        name = container_name(task_id)
        await self.remove(name)

    async def remove_project_containers(self, project_id: str) -> None:
        """Stops and removes all containers belonging to a project."""
        try:
            res = await self._runner.run(
                [
                    "ps",
                    "-a",
                    "--filter",
                    f"label={APP_NAME}.project={project_id}",
                    "--format",
                    "{{.ID}}",
                ]
            )
        except Exception:
            return
        if res.exit_code == 0 and res.stdout.strip():
            for cid in res.stdout.strip().splitlines():
                cid = cid.strip()
                if cid:
                    await self.remove(cid)

    async def cleanup_all(self) -> None:
        """Stops and removes all containers created by the app."""
        try:
            res = await self._runner.run(
                ["ps", "-a", "--filter", f"label={APP_NAME}=true", "--format", "{{.ID}}"]
            )
        except Exception:
            return
        if res.exit_code == 0 and res.stdout.strip():
            for cid in res.stdout.strip().splitlines():
                cid = cid.strip()
                if cid:
                    await self.remove(cid)

    async def cleanup_orphans(self) -> None:
        """Removes leftover containers at startup. Does not fail if Docker is unavailable."""
        await self.cleanup_all()

    async def ensure_container(
        self,
        project_id: str,
        task: TaskRef,
        image_tag: str,
        *,
        network: bool = False,
        is_posix: bool | None = None,
    ) -> str:
        """Ensures a container is running for the task on the specified image and network setting."""
        name = container_name(task.id)
        info = await self.inspect(name)
        if info is not None:
            if info.running and info.image_tag == image_tag and info.network == network:
                return name
            await self.remove(name)

        return await self.start(
            project_id,
            task,
            image_tag,
            network=network,
            is_posix=is_posix,
        )
