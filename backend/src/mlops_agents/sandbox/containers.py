"""Task container management for Docker sandbox."""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from typing import cast

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.config import APP_NAME
from mlops_agents.sandbox.contract import SandboxUnavailable
from mlops_agents.sandbox.docker import DOCKER_INSTALL_MESSAGE, DockerRunner


def container_name(task_id: str) -> str:
    """Returns the container name for a task."""
    return f"{APP_NAME}-{task_id}"


def build_run_args(
    project_id: str,
    task: TaskRef,
    image_tag: str,
    *,
    network: bool = False,
    user: str | None = None,
) -> list[str]:
    """Builds docker run arguments for starting a task container."""
    name = container_name(task.id)
    args = [
        "run",
        "-d",
        "--init",
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
    if user is not None:
        args.extend(["-u", user])
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
    paused: bool = False


class ContainerManager:
    """Manages Docker containers for tasks."""

    def __init__(self, runner: DockerRunner, *, user: str | None = None) -> None:
        self._runner = runner
        self._user = user

    async def inspect(self, name: str) -> ContainerInfo | None:
        """Inspects a container and returns its info, or None if not found."""
        res = await self._runner.run(["inspect", name], timeout_s=15.0)
        if res.exit_code != 0:
            return None
        try:
            raw_data: object = json.loads(res.stdout)
            if not isinstance(raw_data, list) or not raw_data:
                raise SandboxUnavailable(
                    f"Failed to parse inspect output for container {name}: empty or non-list JSON"
                )
            first_raw = cast(list[object], raw_data)[0]
            if not isinstance(first_raw, dict):
                raise SandboxUnavailable(
                    f"Failed to parse inspect output for container {name}: expected JSON object"
                )
            item = cast(dict[str, object], first_raw)
        except json.JSONDecodeError as err:
            raise SandboxUnavailable(
                f"Failed to parse inspect output for container {name}: {err}"
            ) from err

        state_raw = item.get("State")
        state = cast(dict[str, object], state_raw) if isinstance(state_raw, dict) else {}
        running = bool(state.get("Running", False))
        paused = bool(state.get("Paused", False))

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
            paused=paused,
        )

    async def start(
        self,
        project_id: str,
        task: TaskRef,
        image_tag: str,
        *,
        network: bool = False,
    ) -> str:
        """Starts a new container for the task and returns its name."""
        name = container_name(task.id)
        await self.remove(name)
        args = build_run_args(
            project_id,
            task,
            image_tag,
            network=network,
            user=self._user,
        )
        try:
            res = await self._runner.run(args, timeout_s=30.0)
        except FileNotFoundError as err:
            raise SandboxUnavailable(DOCKER_INSTALL_MESSAGE) from err
        except TimeoutError as err:
            raise SandboxUnavailable("Timed out starting sandbox container.") from err
        if res.exit_code != 0:
            detail = res.stderr.strip() or res.stdout.strip() or "docker run failed"
            raise SandboxUnavailable(f"Failed to start sandbox container: {detail}")
        return name

    async def pause(self, name: str) -> None:
        """Pauses a running container."""
        try:
            res = await self._runner.run(["pause", name], timeout_s=15.0)
        except FileNotFoundError as err:
            raise SandboxUnavailable(DOCKER_INSTALL_MESSAGE) from err
        except TimeoutError as err:
            raise SandboxUnavailable(f"Timed out pausing container {name}.") from err
        except OSError as err:
            raise SandboxUnavailable(f"Failed to pause container {name}: {err}") from err
        if res.exit_code != 0:
            detail = (res.stderr or res.stdout).strip()
            raise SandboxUnavailable(f"Failed to pause sandbox container {name}: {detail}")

    async def unpause(self, name: str) -> None:
        """Unpauses a paused container."""
        try:
            res = await self._runner.run(["unpause", name], timeout_s=15.0)
        except FileNotFoundError as err:
            raise SandboxUnavailable(DOCKER_INSTALL_MESSAGE) from err
        except TimeoutError as err:
            raise SandboxUnavailable(f"Timed out unpausing sandbox container {name}.") from err
        except OSError as err:
            raise SandboxUnavailable(f"Failed to unpause sandbox container {name}: {err}") from err
        if res.exit_code != 0:
            detail = (res.stderr or res.stdout).strip()
            raise SandboxUnavailable(f"Failed to unpause sandbox container {name}: {detail}")

    async def remove(self, name: str) -> None:
        """Removes a container by name if it exists."""
        try:
            res = await self._runner.run(["rm", "-f", name], timeout_s=30.0)
        except FileNotFoundError:
            # Removal is already satisfied if Docker has been uninstalled.
            return
        except TimeoutError as err:
            raise SandboxUnavailable(f"Timed out removing sandbox container {name}.") from err
        except OSError as err:
            raise SandboxUnavailable(f"Failed to remove sandbox container {name}: {err}") from err
        if res.exit_code != 0:
            detail = (res.stderr or res.stdout).strip()
            if "no such container" in detail.lower():
                return
            raise SandboxUnavailable(f"Failed to remove sandbox container {name}: {detail}")

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
                ],
                timeout_s=15.0,
            )
        except FileNotFoundError:
            return
        if res.exit_code == 0 and res.stdout.strip():
            for cid in res.stdout.strip().splitlines():
                cid = cid.strip()
                if cid:
                    with contextlib.suppress(SandboxUnavailable):
                        await self.remove(cid)

    async def cleanup_all(self) -> None:
        """Stops and removes all containers created by the app."""
        try:
            res = await self._runner.run(
                ["ps", "-a", "--filter", f"label={APP_NAME}=true", "--format", "{{.ID}}"],
                timeout_s=15.0,
            )
        except FileNotFoundError:
            return
        if res.exit_code == 0 and res.stdout.strip():
            for cid in res.stdout.strip().splitlines():
                cid = cid.strip()
                if cid:
                    with contextlib.suppress(SandboxUnavailable):
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
        )


def host_container_user() -> str | None:
    import os

    if os.name == "posix":
        return f"{os.getuid()}:{os.getgid()}"
    return None
