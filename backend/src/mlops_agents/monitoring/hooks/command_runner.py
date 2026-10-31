"""Command runner for action hooks executing in one-shot sandbox containers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mlops_agents.agents.approvals.authority import Authority
from mlops_agents.credentials.keystore import KeyStore
from mlops_agents.credentials.project_secrets import hook_ref
from mlops_agents.credentials.scrub import scrub_url_secrets, scrub_values
from mlops_agents.ids import new_id
from mlops_agents.monitoring.hooks.constants import (
    HOOK_COMMAND_TIMEOUT_S,
    HOOK_CREDENTIAL_ENV,
    HOOK_DETAIL_MAX_CHARS,
)
from mlops_agents.monitoring.hooks.contract import HookResult, HookRunner, HookView
from mlops_agents.proposals.templates import fill_template
from mlops_agents.sandbox.containers import build_one_shot_run_args
from mlops_agents.sandbox.contract import Sandbox, SandboxUnavailable
from mlops_agents.sandbox.docker import (
    DockerRunner,
    DockerTimeoutError,
    docker_error_boundary,
)
from mlops_agents.sandbox.projects import Mount, SandboxProjects

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Mount:
    host_path: Path
    container_path: str


class CommandRunner(HookRunner):
    """Executes a command hook in a one-shot Docker sandbox container."""

    type: Literal["command", "ci_dispatch"] = "command"

    def __init__(
        self,
        *,
        sandbox: Sandbox,
        projects: SandboxProjects,
        docker_runner: DockerRunner,
        keystore: KeyStore,
    ) -> None:
        self._sandbox = sandbox
        self._projects = projects
        self._docker_runner = docker_runner
        self._keystore = keystore

    async def run(
        self,
        project_id: str,
        hook: HookView,
        params: Mapping[str, str],
        authority: Authority,
    ) -> HookResult:
        request_id = params.get("request_id")

        # 1. Quoting and template filling (happens before any Docker operation)
        quoted_values = {k: shlex.quote(str(v)) for k, v in params.items()}
        template = hook.spec.template or ""
        filled_cmd = fill_template(template, quoted_values)

        # 2. Credential retrieval from KeyStore
        cred_ref = hook_ref(project_id, hook.id)
        credential = await asyncio.to_thread(self._keystore.get, cred_ref)

        # 3. Ensure image is ready
        try:
            env_status = await self._sandbox.rebuild(project_id)
        except SandboxUnavailable as err:
            return HookResult(ok=False, detail=str(err), request_id=request_id)

        if env_status.state != "ready" or not env_status.tag:
            detail = env_status.detail or f"Sandbox image not ready (state={env_status.state})"
            return HookResult(ok=False, detail=detail, request_id=request_id)

        image_tag = env_status.tag

        # 4. Prepare mounts
        root = await self._projects.root(project_id)
        conn_mounts = await self._projects.mounts(project_id)
        workspace_mount = _Mount(host_path=root, container_path="/workspace")
        mounts: list[Mount] = [workspace_mount, *conn_mounts]

        # 5. Temporary env file if credential is present
        env_file_path: Path | None = None
        if credential:
            with tempfile.NamedTemporaryFile(
                mode="w",
                delete=False,
                prefix="hook_env_",
                suffix=".env",
                encoding="utf-8",
            ) as temp_file:
                env_file_path = Path(temp_file.name)
                temp_file.write(f"{HOOK_CREDENTIAL_ENV}={credential}\n")
                temp_file.flush()
            if hasattr(os, "chmod"):
                with contextlib.suppress(OSError):
                    env_file_path.chmod(0o600)

        command = ["bash", "-lc", filled_cmd]
        run_id = params.get("run_id") or new_id("hok")
        container_name = f"hook-{run_id}"
        run_args = build_one_shot_run_args(
            project_id,
            image_tag,
            command,
            network=True,
            mounts=mounts,
            env_file=env_file_path,
            name=container_name,
        )

        secrets = [credential] if credential else []

        try:
            try:
                async with docker_error_boundary("run hook container", reraise_docker_timeout=True):
                    res = await self._docker_runner.run(
                        run_args,
                        timeout_s=float(HOOK_COMMAND_TIMEOUT_S),
                        secrets=secrets,
                    )
                ok = res.exit_code == 0
                output, _ = res.command_output()
                raw_detail = output
            except DockerTimeoutError as err:
                try:
                    await self._docker_runner.run(["rm", "-f", container_name], timeout_s=15.0)
                except Exception:
                    logger.exception("Failed to remove timed-out hook container %s", container_name)
                ok = False
                raw_detail = err.output or f"{err.stdout}{err.stderr}" or str(err)
            except asyncio.CancelledError:
                try:
                    await self._docker_runner.run(["rm", "-f", container_name], timeout_s=15.0)
                except Exception:
                    logger.exception("Failed to remove cancelled hook container %s", container_name)
                raise
            except SandboxUnavailable as err:
                return HookResult(ok=False, detail=str(err), request_id=request_id)

            detail = scrub_values(scrub_url_secrets(raw_detail), secrets)
            if len(detail) > HOOK_DETAIL_MAX_CHARS:
                detail = detail[-HOOK_DETAIL_MAX_CHARS:]

            return HookResult(
                ok=ok,
                detail=detail,
                ci_run_url=None,
                request_id=request_id,
            )
        finally:
            if env_file_path is not None:
                with contextlib.suppress(OSError):
                    env_file_path.unlink(missing_ok=True)
