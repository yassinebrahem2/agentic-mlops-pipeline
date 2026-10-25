"""Build context creation, tag calculation, docker build, and tag pruning."""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import tempfile
from importlib import resources
from pathlib import Path

from mlops_agents.config import APP_NAME
from mlops_agents.sandbox.contract import EnvironmentStatus
from mlops_agents.sandbox.docker import DockerRunner
from mlops_agents.sandbox.environment import (
    DEFAULT_PYTHON_VERSION,
    VALID_PYTHON_VERSION_RE,
    DetectedEnvironment,
    is_safe_regular_file,
)

BUILD_TIMEOUT_S = 15 * 60  # 15 minutes (900 seconds)
FAIL_DETAIL_LINES = 50


def load_template(template_name: str, python_version: str) -> str:
    """Loads a Dockerfile template and injects the target Python version literally."""
    if not VALID_PYTHON_VERSION_RE.match(python_version):
        python_version = DEFAULT_PYTHON_VERSION

    template_file = f"{template_name}.Dockerfile"
    try:
        content = (
            resources.files("mlops_agents.sandbox.templates")
            .joinpath(template_file)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, TypeError):
        path = Path(__file__).parent / "templates" / template_file
        content = path.read_text(encoding="utf-8")

    # Substitute literally line by line to prevent any regex or newline injection
    lines: list[str] = []
    for line in content.splitlines(keepends=True):
        if line.startswith("ARG PYTHON_VERSION="):
            lines.append(f"ARG PYTHON_VERSION={python_version}\n")
        else:
            lines.append(line)
    return "".join(lines)


def compute_image_tag(project_id: str, template_content: str, files: dict[str, bytes]) -> str:
    """Computes a stable image tag: <app name>-env:<project id>-<12 hex of sha256>."""
    hasher = hashlib.sha256()
    hasher.update(template_content.encode("utf-8"))
    for filename in sorted(files.keys()):
        hasher.update(filename.encode("utf-8"))
        hasher.update(files[filename])
    hex12 = hasher.hexdigest()[:12]
    return f"{APP_NAME}-env:{project_id}-{hex12}"


def create_build_context(worktree: Path, detected: DetectedEnvironment, target_dir: Path) -> Path:
    """Populates build context with ONLY the template Dockerfile and regular dependency files."""
    target_dir.mkdir(parents=True, exist_ok=True)
    dockerfile_content = load_template(detected.template, detected.python_version)
    (target_dir / "Dockerfile").write_text(dockerfile_content, encoding="utf-8")

    for dep_file in detected.dependency_files:
        if is_safe_regular_file(worktree, dep_file):
            shutil.copy2(worktree / dep_file, target_dir / dep_file)

    return target_dir


class ImageManager:
    """Manages Docker image lifecycle for project environments."""

    def __init__(self, runner: DockerRunner) -> None:
        self._runner = runner

    def tag_for(self, project_id: str, worktree: Path, detected: DetectedEnvironment) -> str:
        template_content = load_template(detected.template, detected.python_version)
        files = {
            f: (worktree / f).read_bytes()
            for f in detected.dependency_files
            if is_safe_regular_file(worktree, f)
        }
        return compute_image_tag(project_id, template_content, files)

    async def image_exists(self, tag: str) -> bool:
        try:
            res = await self._runner.run(["image", "inspect", tag], timeout_s=15.0)
            return res.exit_code == 0
        except Exception:
            return False

    async def build_image(
        self, project_id: str, worktree: Path, detected: DetectedEnvironment
    ) -> EnvironmentStatus:
        tag = self.tag_for(project_id, worktree, detected)
        if await self.image_exists(tag):
            return EnvironmentStatus(state="ready", tag=tag, detail=None)

        with tempfile.TemporaryDirectory() as tmp_dir:
            context_path = Path(tmp_dir)
            create_build_context(worktree, detected, context_path)

            try:
                res = await self._runner.run(
                    ["build", "-t", tag, str(context_path)],
                    timeout_s=BUILD_TIMEOUT_S,
                )
            except FileNotFoundError:
                return EnvironmentStatus(
                    state="failed",
                    tag=tag,
                    detail="Docker is not installed or not on PATH. Install Docker Desktop (https://www.docker.com/products/docker-desktop) or Docker Engine.",
                )
            except TimeoutError as err:
                detail = f"Image build timed out after {BUILD_TIMEOUT_S}s."
                stderr = getattr(err, "stderr", None)
                if isinstance(stderr, str) and stderr:
                    detail = f"{detail}\n{stderr}"
                return EnvironmentStatus(
                    state="failed",
                    tag=tag,
                    detail=detail,
                )
            except Exception as exc:
                return EnvironmentStatus(
                    state="failed",
                    tag=tag,
                    detail=f"Image build failed: {exc}",
                )

        if res.exit_code != 0:
            full_output = (res.stdout + "\n" + res.stderr).strip()
            lines = full_output.splitlines()
            detail = (
                "\n".join(lines[-FAIL_DETAIL_LINES:])
                if len(lines) > FAIL_DETAIL_LINES
                else full_output
            )
            return EnvironmentStatus(state="failed", tag=tag, detail=detail)

        await self.prune_project_tags(project_id, keep_tag=tag)
        return EnvironmentStatus(state="ready", tag=tag, detail=None)

    async def prune_project_tags(self, project_id: str, keep_tag: str) -> None:
        try:
            res = await self._runner.run(
                ["images", "--format", "{{.Repository}}:{{.Tag}}"], timeout_s=15.0
            )
        except Exception:
            return

        if res.exit_code != 0:
            return

        prefix = f"{APP_NAME}-env:{project_id}-"
        for line in res.stdout.splitlines():
            tag = line.strip()
            if not tag.startswith(prefix) or tag == keep_tag:
                continue

            # Check if any container uses this older image tag
            with contextlib.suppress(Exception):
                ps_res = await self._runner.run(
                    ["ps", "-a", "--filter", f"ancestor={tag}", "--format", "{{.ID}}"],
                    timeout_s=15.0,
                )
                if ps_res.exit_code == 0 and not ps_res.stdout.strip():
                    await self._runner.run(["image", "rm", tag], timeout_s=30.0)
