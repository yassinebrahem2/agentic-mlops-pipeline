from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Iterable, Sequence
from pathlib import Path
from typing import Any, cast

from mlops_agents.config import APP_NAME
from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog, EventRecord, EventType, Resync
from mlops_agents.sandbox.docker import DockerResult, DockerRunner, DockerTimeoutError


class MemorySandboxProjects:
    def __init__(self, roots: dict[str, Path] | None = None) -> None:
        self.roots = roots or {}
        self.network: dict[str, bool] = {}

    async def root(self, project_id: str) -> Path:
        if project_id not in self.roots:
            raise DomainError("project_not_found", "This project does not exist.", status=404)
        return self.roots[project_id]

    async def get_network(self, project_id: str) -> bool:
        return self.network.get(project_id, False)

    async def set_network(self, project_id: str, enabled: bool) -> None:
        self.network[project_id] = enabled


class FakeEventLog(EventLog):
    def __init__(self) -> None:
        self.records: list[EventRecord] = []

    async def record(
        self,
        project_id: str,
        *,
        source: str,
        type: EventType,
        payload: dict[str, Any],
        subject_id: str | None = None,
    ) -> EventRecord:
        rec = EventRecord(
            id=f"evt_{len(self.records) + 1}",
            seq=len(self.records) + 1,
            ts="2026-10-25T00:00:00Z",
            source=source,
            type=type,
            subject_id=subject_id,
            payload=payload,
        )
        self.records.append(rec)
        return rec

    async def _empty_stream(self) -> AsyncGenerator[EventRecord | Resync, None]:
        for _ in ():
            yield cast(EventRecord, None)

    def subscribe(
        self, project_id: str, after_seq: int | None
    ) -> AsyncGenerator[EventRecord | Resync, None]:
        return self._empty_stream()

    async def history(
        self, project_id: str, *, types: Sequence[EventType], limit: int = 200
    ) -> list[EventRecord]:
        return [r for r in self.records if r.type in types][:limit]


class FakeDockerRunner(DockerRunner):
    """A unified fake Docker runner for tests."""

    def __init__(
        self,
        *,
        daemon_down: bool = False,
        not_installed: bool = False,
        raise_not_found: bool = False,
        containers: dict[str, dict[str, object]] | None = None,
        built_tags: set[str] | None = None,
        fail_build: bool = False,
        build_fail_detail: str | None = None,
        build_fail_output: str | None = None,
        build_timeout: bool = False,
        raise_timeout: bool = False,
        default_build_delay: float = 0.0,
        build_delays: dict[str, float] | None = None,
        fail_run: bool = False,
        exec_result: DockerResult | None = None,
        exec_timeout: bool = False,
        exec_timeout_output: str = "",
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
    ) -> None:
        self.daemon_down = daemon_down
        self.not_installed = not_installed or raise_not_found
        self.calls: list[list[str]] = []
        self.timeouts: list[float | None] = []
        self.containers: dict[str, dict[str, object]] = containers or {}
        self.built_tags: set[str] = built_tags if built_tags is not None else set()
        self.existing_images = self.built_tags
        self.images = self.built_tags
        self.containers_by_image: dict[str, list[str]] = {}
        self.fail_build = fail_build
        self.build_fail_detail = build_fail_detail
        self.build_fail_output = build_fail_output
        self.build_timeout = build_timeout
        self.raise_timeout = raise_timeout
        self.default_build_delay = default_build_delay
        self.build_delays: dict[str, float] = build_delays or {}
        self.build_calls: int = 0
        self.fail_run = fail_run
        self.exec_result = exec_result
        self.exec_timeout = exec_timeout
        self.exec_timeout_output = exec_timeout_output
        self.default_stdout = stdout
        self.default_stderr = stderr
        self.default_exit_code = exit_code
        self._container_counter: int = len(self.containers)

    @property
    def docker_installed(self) -> bool:
        return not self.not_installed

    @docker_installed.setter
    def docker_installed(self, value: bool) -> None:
        self.not_installed = not value

    @property
    def daemon_running(self) -> bool:
        return not self.daemon_down

    @daemon_running.setter
    def daemon_running(self, value: bool) -> None:
        self.daemon_down = not value

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None = None,
        secrets: Iterable[str] = (),
    ) -> DockerResult:
        cmd = list(args)
        self.calls.append(cmd)
        self.timeouts.append(timeout_s)

        if self.not_installed:
            raise FileNotFoundError("docker not found")

        if not cmd:
            return DockerResult(stdout="", stderr="", exit_code=0)

        if self.daemon_down:
            if cmd[0] == "version":
                return DockerResult(
                    stdout=json.dumps({"Client": {"Version": "27.0.0"}}),
                    stderr="Cannot connect to the Docker daemon. Is the docker daemon running?",
                    exit_code=1,
                )
            return DockerResult(
                stdout="",
                stderr="Cannot connect to the Docker daemon. Is the docker daemon running?",
                exit_code=1,
            )

        if cmd[0] == "version":
            if self.default_stdout or self.default_stderr or self.default_exit_code:
                return DockerResult(
                    stdout=self.default_stdout,
                    stderr=self.default_stderr,
                    exit_code=self.default_exit_code,
                )
            data = {"Client": {"Version": "27.0.0"}, "Server": {"Version": "27.0.0"}}
            return DockerResult(stdout=json.dumps(data), stderr="", exit_code=0)

        if cmd[0] == "image" and len(cmd) > 1 and cmd[1] == "inspect":
            tag = cmd[2] if len(cmd) > 2 else ""
            if tag in self.built_tags:
                return DockerResult(stdout="{}", stderr="", exit_code=0)
            return DockerResult(stdout="", stderr=f"Error: No such image: {tag}", exit_code=1)

        if cmd[0] == "build":
            self.build_calls += 1
            if self.build_timeout or self.raise_timeout:
                raise DockerTimeoutError(
                    "Docker build timed out", stderr="dependency download stalled"
                )
            if self.build_fail_output is not None:
                return DockerResult(stdout="", stderr=self.build_fail_output, exit_code=1)
            if self.build_fail_detail is not None:
                return DockerResult(stdout="", stderr=self.build_fail_detail, exit_code=1)
            if self.fail_build:
                return DockerResult(
                    stdout="", stderr="Syntax error in Dockerfile\nLine 2 failed", exit_code=1
                )
            tag = ""
            if "-t" in cmd:
                tag = cmd[cmd.index("-t") + 1]
                self.built_tags.add(tag)
            delay = self.build_delays.get(tag, self.default_build_delay)
            if delay > 0:
                await asyncio.sleep(delay)
            return DockerResult(stdout="Successfully built", stderr="", exit_code=0)

        if cmd[0] == "images":
            tags = [img for img in self.built_tags if img.startswith(f"{APP_NAME}-env:")] or list(
                self.built_tags
            )
            return DockerResult(stdout="\n".join(tags), stderr="", exit_code=0)

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
            name = cmd[cmd.index("--name") + 1] if "--name" in cmd else f"c_{len(self.containers)}"
            labels: dict[str, str] = {}
            for i, arg in enumerate(cmd):
                if arg == "--label" and i + 1 < len(cmd):
                    k, _, v = cmd[i + 1].partition("=")
                    labels[k] = v
            net_mode = (
                "none"
                if "--network" in cmd and cmd[cmd.index("--network") + 1] == "none"
                else "default"
            )
            tag = ""
            if "sleep" in cmd:
                tag = cmd[cmd.index("sleep") - 1]
            elif len(cmd) >= 2:
                tag = cmd[-2]
            self._container_counter += 1
            self.containers[name] = {
                "Id": f"cid_{name}_{self._container_counter}",
                "Name": f"/{name}",
                "State": {"Running": True, "Paused": False, "Status": "running"},
                "Config": {"Labels": labels, "Image": tag},
                "HostConfig": {"NetworkMode": net_mode},
            }
            return DockerResult(stdout=name, stderr="", exit_code=0)

        if cmd[0] == "pause":
            name = cmd[1]
            if name in self.containers:
                state = cast(dict[str, object], self.containers[name]["State"])
                state["Paused"] = True
                return DockerResult(stdout=name, stderr="", exit_code=0)
            return DockerResult(stdout="", stderr=f"Error: No such container: {name}", exit_code=1)

        if cmd[0] == "unpause":
            name = cmd[1]
            if name in self.containers:
                state = cast(dict[str, object], self.containers[name]["State"])
                state["Paused"] = False
                return DockerResult(stdout=name, stderr="", exit_code=0)
            return DockerResult(stdout="", stderr=f"Error: No such container: {name}", exit_code=1)

        if cmd[0] == "rm":
            for target in cmd[1:]:
                if not target.startswith("-"):
                    self.containers.pop(target, None)
            return DockerResult(stdout="Deleted", stderr="", exit_code=0)

        if cmd[0] == "restart":
            name = cmd[-1]
            if name in self.containers:
                state = cast(dict[str, object], self.containers[name]["State"])
                state["Running"] = True
                state["Paused"] = False
            return DockerResult(stdout="restarted\n", stderr="", exit_code=0)

        if cmd[0] == "ps":
            matching_ids: list[str] = []
            filter_label: str | None = None
            filter_ancestor: str | None = None
            for i, arg in enumerate(cmd):
                if arg == "--filter" and i + 1 < len(cmd):
                    fval = cmd[i + 1]
                    if fval.startswith("label="):
                        filter_label = fval.partition("=")[2]
                    elif fval.startswith("ancestor="):
                        filter_ancestor = fval.partition("=")[2]
                elif arg.startswith("ancestor="):
                    filter_ancestor = arg.partition("=")[2]
            if filter_ancestor is not None and filter_ancestor in self.containers_by_image:
                return DockerResult(
                    stdout="\n".join(self.containers_by_image[filter_ancestor]),
                    stderr="",
                    exit_code=0,
                )
            for cname, cinfo in self.containers.items():
                if filter_label is not None:
                    cfg = cast(dict[str, object], cinfo.get("Config", {}))
                    lbls = cast(dict[str, str], cfg.get("Labels", {}))
                    lk, _, lv = filter_label.partition("=")
                    if lk not in lbls or (lv and lbls[lk] != lv):
                        continue
                if filter_ancestor is not None:
                    cfg = cast(dict[str, object], cinfo.get("Config", {}))
                    if cfg.get("Image") != filter_ancestor:
                        continue
                matching_ids.append(cname)
            return DockerResult(stdout="\n".join(matching_ids), stderr="", exit_code=0)

        if cmd[0] == "exec":
            if self.exec_timeout:
                raise DockerTimeoutError(
                    "Command timed out",
                    stdout=self.exec_timeout_output,
                    stderr="",
                    output=self.exec_timeout_output,
                    truncated=False,
                )
            if self.exec_result is not None:
                return self.exec_result
            return DockerResult(stdout="executed\n", stderr="", exit_code=0)

        if cmd[0] in ("image", "rmi"):
            return DockerResult(stdout="Deleted", stderr="", exit_code=0)

        return DockerResult(
            stdout=self.default_stdout,
            stderr=self.default_stderr,
            exit_code=self.default_exit_code,
        )
