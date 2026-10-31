"""Code parser runner in isolated sandbox containers (I22)."""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol, cast

from mlops_agents.connections.contract import Mount
from mlops_agents.ids import new_id
from mlops_agents.monitoring.results.contract import (
    ParseOutcome,
    ParserCode,
    ResultRecord,
)
from mlops_agents.sandbox.containers import build_one_shot_run_args
from mlops_agents.sandbox.contract import EnvironmentStatus
from mlops_agents.sandbox.docker import DockerRunner

logger = logging.getLogger(__name__)


class _Connections(Protocol):
    async def mounts(self, project_id: str) -> list[Mount]: ...


class _Sandbox(Protocol):
    async def rebuild(self, project_id: str) -> EnvironmentStatus: ...


class CodeParserRunner:
    """Executes a ParserCode script inside an isolated sandbox container."""

    def __init__(
        self,
        sandbox: _Sandbox,
        docker_runner: DockerRunner,
        connections: _Connections,
    ) -> None:
        self._sandbox = sandbox
        self._docker_runner = docker_runner
        self._connections = connections

    async def run(
        self,
        project_id: str,
        code: ParserCode,
        raw: str,
        origin_ref: str,
    ) -> ParseOutcome:
        env_status = await self._sandbox.rebuild(project_id)
        if env_status.tag is None or env_status.state != "ready":
            raise RuntimeError(
                f"Sandbox unavailable for project {project_id}: {env_status.detail or env_status.state}"
            )

        image_tag = env_status.tag
        mounts = await self._connections.mounts(project_id)

        container_name = f"parser-{new_id('res')}"
        command = ["python", "-c", code.script, origin_ref, raw]

        run_args = build_one_shot_run_args(
            project_id=project_id,
            image_tag=image_tag,
            command=command,
            network=False,
            mounts=mounts,
            env_file=None,
            name=container_name,
        )

        res = await self._docker_runner.run(run_args, timeout_s=float(code.timeout_s))
        if res.exit_code != 0:
            stdout, stderr = res.command_output()
            err_msg = stderr or stdout or f"Container exited with code {res.exit_code}"
            raise RuntimeError(f"Code parser execution failed: {err_msg}")

        stdout, _ = res.command_output()
        records: list[tuple[str, ResultRecord]] = []
        errors: list[str] = []

        stdout_trimmed = stdout.strip()
        if not stdout_trimmed:
            return ParseOutcome(records=[], errors=[])

        # Try parsing as JSON array first, else JSON Lines
        items: list[dict[str, Any]] = []
        try:
            parsed: object = json.loads(stdout_trimmed)
            if isinstance(parsed, list):
                parsed_list = cast(list[object], parsed)
                for it in parsed_list:
                    if isinstance(it, dict):
                        items.append(cast(dict[str, Any], it))
            elif isinstance(parsed, dict):
                items.append(cast(dict[str, Any], parsed))
        except Exception:
            for line in stdout_trimmed.splitlines():
                l_str = line.strip()
                if not l_str:
                    continue
                try:
                    p: object = json.loads(l_str)
                    if isinstance(p, dict):
                        items.append(cast(dict[str, Any], p))
                    else:
                        errors.append(f"Line is not a dict: {l_str}")
                except Exception as exc:
                    errors.append(f"Invalid JSON line: {exc}")

        for idx, item in enumerate(items):
            try:
                item_dict = dict(item)
                item_dict["origin"] = "team"
                if not item_dict.get("source_ref"):
                    item_dict["source_ref"] = origin_ref
                custom_key = item_dict.pop("source_key", None) or item_dict.pop("key", None)
                source_key = str(custom_key) if custom_key is not None else f"{origin_ref}:{idx}"
                rec = ResultRecord.model_validate(item_dict)
                records.append((source_key, rec))
            except Exception as exc:
                errors.append(f"Record {idx}: {exc}")

        return ParseOutcome(records=records, errors=errors)
