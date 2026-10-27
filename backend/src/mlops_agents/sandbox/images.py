"""Build context creation, tag calculation, docker build, and tag pruning."""

from __future__ import annotations

import hashlib
import logging
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import MappingProxyType

from mlops_agents.config import APP_NAME
from mlops_agents.sandbox.contract import EnvironmentStatus, SandboxUnavailable
from mlops_agents.sandbox.dependencies import read_dependency_files
from mlops_agents.sandbox.docker import (
    DOCKER_INSTALL_MESSAGE,
    DockerRunner,
    DockerTimeoutError,
    docker_error_boundary,
)
from mlops_agents.sandbox.environment import (
    DEFAULT_PYTHON_VERSION,
    VALID_PYTHON_VERSION_RE,
    DetectedEnvironment,
    environment_from_files,
)

logger = logging.getLogger(__name__)

BUILD_TIMEOUT_S = 15 * 60  # 15 minutes (900 seconds)
FAIL_DETAIL_LINES = 50


def load_template(template_name: str, python_version: str) -> str:
    """Loads a Dockerfile template and injects the target Python version literally."""
    if not VALID_PYTHON_VERSION_RE.fullmatch(python_version):
        python_version = DEFAULT_PYTHON_VERSION

    template_file = f"{template_name}.Dockerfile"
    content = (
        resources.files("mlops_agents.sandbox.templates")
        .joinpath(template_file)
        .read_text(encoding="utf-8")
    )

    # Substitute literally line by line to prevent any regex or newline injection
    lines: list[str] = []
    for line in content.splitlines(keepends=True):
        if line.startswith("ARG PYTHON_VERSION="):
            lines.append(f"ARG PYTHON_VERSION={python_version}\n")
        else:
            lines.append(line)
    return "".join(lines)


def compute_image_tag(project_id: str, template_content: str, files: Mapping[str, bytes]) -> str:
    """Computes a stable image tag: <app name>-env:<project id>-<12 hex of sha256>."""
    hasher = hashlib.sha256()
    hasher.update(template_content.encode("utf-8"))
    for filename in sorted(files.keys()):
        hasher.update(filename.encode("utf-8"))
        hasher.update(files[filename])
    hex12 = hasher.hexdigest()[:12]
    return f"{APP_NAME}-env:{project_id}-{hex12}"


@dataclass(frozen=True)
class ImageInputs:
    tag: str
    template: str
    files: Mapping[str, bytes]
    template_name: str


def snapshot_image(
    project_id: str, worktree: Path, detected: DetectedEnvironment | None = None
) -> ImageInputs:
    files = read_dependency_files(worktree)
    detected = detected or environment_from_files(files)
    template = load_template(detected.template, detected.python_version)
    dependencies = {name: files[name] for name in detected.dependency_files if name in files}
    return ImageInputs(
        compute_image_tag(project_id, template, dependencies),
        template,
        MappingProxyType(dependencies),
        detected.template,
    )


def create_build_context(inputs: ImageInputs, target_dir: Path) -> Path:
    """Write exactly the bytes used for the image tag, without reopening worktree paths."""
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "Dockerfile").write_text(inputs.template, encoding="utf-8")
    for name, content in inputs.files.items():
        (target_dir / name).write_bytes(content)
    return target_dir


def build_failure_detail(exc: BaseException) -> str:
    """The one wording for a failed build; it lives here because service imports images."""
    err = str(exc).strip() or type(exc).__name__
    return f"Image build failed: {err}"


class ImageManager:
    """Manages Docker image lifecycle for project environments."""

    def __init__(self, runner: DockerRunner) -> None:
        self._runner = runner

    async def image_exists(self, tag: str) -> bool:
        async with docker_error_boundary(f"inspect image {tag}", gerund=f"inspecting image {tag}"):
            res = await self._runner.run(["image", "inspect", tag], timeout_s=15.0)
        if res.exit_code == 0:
            return True
        err = (res.stderr or res.stdout).strip()
        if "no such image" in err.lower():
            return False
        raise SandboxUnavailable(f"Failed to inspect image {tag}: {err}")

    async def build_image(
        self,
        project_id: str,
        worktree: Path,
        detected: DetectedEnvironment | None = None,
        *,
        inputs: ImageInputs | None = None,
    ) -> EnvironmentStatus:
        inputs = inputs or snapshot_image(project_id, worktree, detected)
        tag = inputs.tag
        if await self.image_exists(tag):
            return EnvironmentStatus(state="ready", tag=tag, detail=None)
        with tempfile.TemporaryDirectory() as tmp_dir:
            context_path = create_build_context(inputs, Path(tmp_dir))
            try:
                res = await self._runner.run(
                    ["build", "-t", tag, str(context_path)],
                    timeout_s=BUILD_TIMEOUT_S,
                )
            except FileNotFoundError:
                return EnvironmentStatus(state="failed", tag=tag, detail=DOCKER_INSTALL_MESSAGE)
            except DockerTimeoutError as exc:
                detail = f"Image build timed out after {BUILD_TIMEOUT_S}s."
                if exc.stderr:
                    detail += "\n" + "\n".join(exc.stderr.splitlines()[-FAIL_DETAIL_LINES:])
                return EnvironmentStatus(state="failed", tag=tag, detail=detail)
            except OSError as exc:
                return EnvironmentStatus(state="failed", tag=tag, detail=build_failure_detail(exc))
        if res.exit_code != 0:
            detail = "\n".join(
                (res.stdout + "\n" + res.stderr).strip().splitlines()[-FAIL_DETAIL_LINES:]
            )
            return EnvironmentStatus(state="failed", tag=tag, detail=detail)
        await self.prune_project_tags(project_id, keep_tag=tag)
        return EnvironmentStatus(state="ready", tag=tag, detail=None)

    async def prune_project_tags(self, project_id: str, keep_tag: str) -> None:
        res = await self._runner.run(
            ["images", "--format", "{{.Repository}}:{{.Tag}}"], timeout_s=15.0
        )
        if res.exit_code != 0:
            logger.warning(
                "Failed to list images for pruning %s: %s", project_id, res.stderr or res.stdout
            )
            return
        prefix = f"{APP_NAME}-env:{project_id}-"
        for line in res.stdout.splitlines():
            tag = line.strip()
            if not tag.startswith(prefix) or tag == keep_tag:
                continue
            ps_res = await self._runner.run(
                ["ps", "-a", "--filter", f"ancestor={tag}", "--format", "{{.ID}}"],
                timeout_s=15.0,
            )
            if ps_res.exit_code != 0:
                logger.warning(
                    "Failed to check containers for image %s: %s",
                    tag,
                    ps_res.stderr or ps_res.stdout,
                )
                continue
            if not ps_res.stdout.strip():
                rm_res = await self._runner.run(["image", "rm", tag], timeout_s=30.0)
                if rm_res.exit_code != 0:
                    logger.warning(
                        "Failed to prune image %s: %s", tag, rm_res.stderr or rm_res.stdout
                    )
