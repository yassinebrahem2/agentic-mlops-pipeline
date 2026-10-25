from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

from mlops_agents.config import APP_NAME
from mlops_agents.sandbox.docker import CliDockerRunner, DockerResult, DockerRunner
from mlops_agents.sandbox.environment import DetectedEnvironment
from mlops_agents.sandbox.images import (
    BUILD_TIMEOUT_S,
    ImageManager,
    compute_image_tag,
    create_build_context,
)


class FakeDocker(DockerRunner):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float | None] = []
        self.existing_images: set[str] = set()
        self.containers_by_image: dict[str, list[str]] = {}
        self.build_fail_output: str | None = None

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

        if cmd[0] == "image" and cmd[1] == "inspect":
            tag = cmd[2]
            if tag in self.existing_images:
                return DockerResult(stdout="{}", stderr="", exit_code=0)
            return DockerResult(stdout="", stderr="Error: No such image", exit_code=1)

        if cmd[0] == "build":
            if self.build_fail_output is not None:
                return DockerResult(stdout="", stderr=self.build_fail_output, exit_code=1)
            # Find the tag in cmd
            tag_idx = cmd.index("-t") + 1
            self.existing_images.add(cmd[tag_idx])
            return DockerResult(stdout="Successfully built", stderr="", exit_code=0)

        if cmd[0] == "images":
            # return lines formatted as repo:tag
            tags = [img for img in self.existing_images if img.startswith(f"{APP_NAME}-env:")]
            return DockerResult(stdout="\n".join(tags), stderr="", exit_code=0)

        if cmd[0] == "ps":
            # ancestor=<tag>
            for arg in cmd:
                if arg.startswith("ancestor="):
                    tag = arg.split("=", 1)[1]
                    containers = self.containers_by_image.get(tag, [])
                    return DockerResult(stdout="\n".join(containers), stderr="", exit_code=0)
            return DockerResult(stdout="", stderr="", exit_code=0)

        if cmd[0] in ("image", "rmi"):
            return DockerResult(stdout="Deleted", stderr="", exit_code=0)

        return DockerResult(stdout="", stderr="", exit_code=0)


def test_compute_image_tag_format_and_stability() -> None:
    template = "FROM python:3.12-slim\nRUN pip install -r requirements.txt\n"
    files = {"requirements.txt": b"pandas==2.0.0\n"}
    tag1 = compute_image_tag("prj_123", template, files)
    tag2 = compute_image_tag("prj_123", template, files)

    assert tag1 == tag2
    assert tag1.startswith(f"{APP_NAME}-env:prj_123-")
    # Tag has 12 hex chars after project prefix
    hex_part = tag1.split("prj_123-")[1]
    assert len(hex_part) == 12
    int(hex_part, 16)  # valid hex


def test_compute_image_tag_changes_on_content_change() -> None:
    template = "FROM python:3.12-slim\n"
    tag1 = compute_image_tag("prj_123", template, {"requirements.txt": b"pandas\n"})
    tag2 = compute_image_tag("prj_123", template, {"requirements.txt": b"numpy\n"})
    tag3 = compute_image_tag("prj_123", template + "# diff\n", {"requirements.txt": b"pandas\n"})

    assert tag1 != tag2
    assert tag1 != tag3


def test_build_context_contains_only_expected_files(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("pandas\n", encoding="utf-8")
    (worktree / "secret.env").write_text("SECRET=12345\n", encoding="utf-8")
    (worktree / "README.md").write_text("# Project\n", encoding="utf-8")
    (worktree / ".git").mkdir()

    detected = DetectedEnvironment(
        template="requirements",
        python_version="3.12",
        dependency_files=("requirements.txt",),
    )

    context_dir = tmp_path / "context"
    create_build_context(worktree, detected, context_dir)

    files = set(f.name for f in context_dir.iterdir())
    assert files == {"Dockerfile", "requirements.txt"}
    assert not (context_dir / "secret.env").exists()
    assert not (context_dir / "README.md").exists()
    assert not (context_dir / ".git").exists()


async def test_build_reuses_existing_image(tmp_path: Path) -> None:
    runner = FakeDocker()
    manager = ImageManager(runner)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("pandas\n", encoding="utf-8")
    detected = DetectedEnvironment(
        template="requirements",
        python_version="3.12",
        dependency_files=("requirements.txt",),
    )

    tag = manager.tag_for("prj_123", worktree, detected)
    runner.existing_images.add(tag)

    status = await manager.build_image("prj_123", worktree, detected)
    assert status.state == "ready"
    assert status.tag == tag
    # Did not call docker build
    assert not any(c[0] == "build" for c in runner.calls)


async def test_build_executes_with_15min_timeout(tmp_path: Path) -> None:
    runner = FakeDocker()
    manager = ImageManager(runner)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("pandas\n", encoding="utf-8")
    detected = DetectedEnvironment(
        template="requirements",
        python_version="3.12",
        dependency_files=("requirements.txt",),
    )

    status = await manager.build_image("prj_123", worktree, detected)
    assert status.state == "ready"
    build_calls = [c for c in runner.calls if c[0] == "build"]
    assert len(build_calls) == 1
    # Check timeout_s was 900 (15 min)
    idx = runner.calls.index(build_calls[0])
    assert runner.timeouts[idx] == BUILD_TIMEOUT_S
    assert runner.timeouts[idx] == 900


async def test_build_failure_retains_last_50_lines(tmp_path: Path) -> None:
    runner = FakeDocker()
    # Generate 70 lines of error output
    lines = [f"Step {i}/10 : RUN pip install ... failed" for i in range(1, 71)]
    runner.build_fail_output = "\n".join(lines)

    manager = ImageManager(runner)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    detected = DetectedEnvironment(
        template="base",
        python_version="3.12",
        dependency_files=(),
    )

    status = await manager.build_image("prj_123", worktree, detected)
    assert status.state == "failed"
    assert status.detail is not None
    detail_lines = status.detail.splitlines()
    assert len(detail_lines) == 50
    assert detail_lines[-1] == "Step 70/10 : RUN pip install ... failed"
    assert detail_lines[0] == "Step 21/10 : RUN pip install ... failed"


async def test_prune_removes_only_unused_older_tags(tmp_path: Path) -> None:
    runner = FakeDocker()
    current_tag = f"{APP_NAME}-env:prj_123-current00000"
    older_unused_tag = f"{APP_NAME}-env:prj_123-unused000000"
    older_in_use_tag = f"{APP_NAME}-env:prj_123-inuse0000000"
    other_prj_tag = f"{APP_NAME}-env:prj_other-000000000"

    runner.existing_images.update([current_tag, older_unused_tag, older_in_use_tag, other_prj_tag])
    runner.containers_by_image[older_in_use_tag] = ["container_abc"]

    manager = ImageManager(runner)
    await manager.prune_project_tags("prj_123", keep_tag=current_tag)

    # Check that older_unused_tag was deleted, older_in_use_tag was NOT deleted, current_tag was NOT deleted
    deleted = [c[2] for c in runner.calls if c[0] == "image" and c[1] == "rm"]
    assert older_unused_tag in deleted
    assert older_in_use_tag not in deleted
    assert current_tag not in deleted
    assert other_prj_tag not in deleted


@pytest.mark.docker
async def test_real_docker_build_and_reuse(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("pip\n", encoding="utf-8")
    detected = DetectedEnvironment(
        template="requirements",
        python_version="3.12",
        dependency_files=("requirements.txt",),
    )
    runner = CliDockerRunner()
    manager = ImageManager(runner)
    project_id = "prj_realdocker"

    status1 = await manager.build_image(project_id, worktree, detected)
    assert status1.state == "ready"
    assert status1.tag is not None

    status2 = await manager.build_image(project_id, worktree, detected)
    assert status2.state == "ready"
    assert status2.tag == status1.tag

    # Cleanup image
    await runner.run(["image", "rm", status1.tag])
