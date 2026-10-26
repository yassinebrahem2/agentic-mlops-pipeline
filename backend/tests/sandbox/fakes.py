from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Iterable, Sequence
from pathlib import Path
from typing import Any, cast

from mlops_agents.errors import DomainError
from mlops_agents.events.contract import EventLog, EventRecord, EventType, Resync
from mlops_agents.sandbox.docker import DockerResult, DockerRunner


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
        containers: dict[str, dict[str, object]] | None = None,
        built_tags: set[str] | None = None,
        fail_build: bool = False,
        default_build_delay: float = 0.0,
        build_delays: dict[str, float] | None = None,
        exec_result: DockerResult | None = None,
    ) -> None:
        self.daemon_down = daemon_down
        self.not_installed = not_installed
        self.calls: list[list[str]] = []
        self.containers: dict[str, dict[str, object]] = containers or {}
        self.built_tags: set[str] = built_tags or set()
        self.fail_build = fail_build
        self.default_build_delay = default_build_delay
        self.build_delays: dict[str, float] = build_delays or {}
        self.build_calls: int = 0
        self.exec_result = exec_result

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float | None = None,
        secrets: Iterable[str] = (),
    ) -> DockerResult:
        cmd = list(args)
        self.calls.append(cmd)

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
            data = {"Client": {"Version": "27.0.0"}, "Server": {"Version": "27.0.0"}}
            return DockerResult(stdout=json.dumps(data), stderr="", exit_code=0)

        if cmd[0] == "image" and len(cmd) > 1 and cmd[1] == "inspect":
            tag = cmd[2] if len(cmd) > 2 else ""
            if tag in self.built_tags:
                return DockerResult(stdout="{}", stderr="", exit_code=0)
            return DockerResult(stdout="", stderr=f"Error: No such image: {tag}", exit_code=1)

        if cmd[0] == "build":
            self.build_calls += 1
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
            return DockerResult(stdout="", stderr="", exit_code=0)

        if cmd[0] == "inspect":
            name = cmd[1]
            if name in self.containers:
                return DockerResult(
                    stdout=json.dumps([self.containers[name]]), stderr="", exit_code=0
                )
            return DockerResult(stdout="", stderr=f"Error: No such container: {name}", exit_code=1)

        if cmd[0] == "run":
            name = ""
            if "--name" in cmd:
                name = cmd[cmd.index("--name") + 1]
            labels: dict[str, str] = {}
            for i, arg in enumerate(cmd):
                if arg == "--label" and i + 1 < len(cmd):
                    k, _, v = cmd[i + 1].partition("=")
                    labels[k] = v
            net = "--network" in cmd and cmd[cmd.index("--network") + 1] != "none"
            tag = cmd[-2] if len(cmd) >= 2 else ""
            self.containers[name] = {
                "State": {"Running": True, "Paused": False},
                "Config": {"Labels": labels, "Image": tag},
                "HostConfig": {"NetworkMode": "default" if net else "none"},
            }
            return DockerResult(stdout=name, stderr="", exit_code=0)

        if cmd[0] == "pause":
            name = cmd[1]
            if name in self.containers:
                state = cast(dict[str, object], self.containers[name]["State"])
                state["Paused"] = True
            return DockerResult(stdout="", stderr="", exit_code=0)

        if cmd[0] == "unpause":
            name = cmd[1]
            if name in self.containers:
                state = cast(dict[str, object], self.containers[name]["State"])
                state["Paused"] = False
            return DockerResult(stdout="", stderr="", exit_code=0)

        if cmd[0] == "rm":
            name = cmd[-1]
            self.containers.pop(name, None)
            return DockerResult(stdout="", stderr="", exit_code=0)

        if cmd[0] == "restart":
            name = cmd[-1]
            if name in self.containers:
                state = cast(dict[str, object], self.containers[name]["State"])
                state["Running"] = True
                state["Paused"] = False
            return DockerResult(stdout="", stderr="", exit_code=0)

        if cmd[0] == "ps":
            output_lines: list[str] = list(self.containers.keys())
            return DockerResult(stdout="\n".join(output_lines), stderr="", exit_code=0)

        if cmd[0] == "exec":
            if self.exec_result is not None:
                return self.exec_result
            return DockerResult(stdout="exec success", stderr="", exit_code=0)

        return DockerResult(stdout="", stderr="", exit_code=0)
