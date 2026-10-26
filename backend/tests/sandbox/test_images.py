from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

from mlops_agents.config import APP_NAME
from mlops_agents.sandbox.docker import DockerResult
from mlops_agents.sandbox.environment import DetectedEnvironment
from mlops_agents.sandbox.images import (
    BUILD_TIMEOUT_S,
    ImageManager,
    compute_image_tag,
    create_build_context,
    load_template,
    snapshot_image,
)
from tests.sandbox.fakes import FakeDockerRunner


async def test_build_context_uses_the_bytes_hashed_before_inspect(tmp_path: Path) -> None:
    from mlops_agents.sandbox.environment import detect_environment

    dependency = tmp_path / "requirements.txt"
    dependency.write_bytes(b"six==1.16.0\n")
    original = dependency.read_bytes()
    context_contents: list[bytes] = []

    class SwappingDocker(FakeDockerRunner):
        async def run(
            self,
            args: Sequence[str],
            *,
            timeout_s: float | None = None,
            secrets: Iterable[str] = (),
        ) -> DockerResult:
            if list(args[:2]) == ["image", "inspect"]:
                dependency.write_bytes(b"secret-host-content\n")
            if args[0] == "build":
                context_contents.append((Path(args[-1]) / "requirements.txt").read_bytes())
            return await super().run(args, timeout_s=timeout_s, secrets=secrets)

    status = await ImageManager(SwappingDocker()).build_image(
        "prj_race", tmp_path, detect_environment(tmp_path)
    )
    assert status.state == "ready"
    assert context_contents == [original]
    assert status.tag == compute_image_tag(
        "prj_race", load_template("requirements", "3.12"), {"requirements.txt": original}
    )


def test_template_version_rejects_trailing_newline() -> None:
    assert load_template("base", "3.11\n") == load_template("base", "3.12")


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
    create_build_context(snapshot_image("prj_123", worktree, detected), context_dir)

    files = set(f.name for f in context_dir.iterdir())
    assert files == {"Dockerfile", "requirements.txt"}
    assert not (context_dir / "secret.env").exists()
    assert not (context_dir / "README.md").exists()
    assert not (context_dir / ".git").exists()


async def test_build_reuses_existing_image(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    manager = ImageManager(runner)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("pandas\n", encoding="utf-8")
    detected = DetectedEnvironment(
        template="requirements",
        python_version="3.12",
        dependency_files=("requirements.txt",),
    )

    tag = snapshot_image("prj_123", worktree, detected).tag
    runner.existing_images.add(tag)

    status = await manager.build_image("prj_123", worktree, detected)
    assert status.state == "ready"
    assert status.tag == tag
    # Did not call docker build
    assert not any(c[0] == "build" for c in runner.calls)


async def test_build_executes_with_15min_timeout(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
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
    runner = FakeDockerRunner()
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
    runner = FakeDockerRunner()
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


async def test_build_timeout_caught_as_failed(tmp_path: Path) -> None:
    runner = FakeDockerRunner()
    runner.raise_timeout = True
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
    assert "timed out" in status.detail
    assert "dependency download stalled" in status.detail


def test_load_template_literal_substitution() -> None:
    content = load_template("base", "3.11")
    assert "ARG PYTHON_VERSION=3.11\n" in content

    # Injected shell commands / newlines must be rejected and fall back to default 3.12
    injected = load_template("base", "3.12\nRUN touch /tmp/pwned")
    assert "ARG PYTHON_VERSION=3.12\n" in injected
    assert "pwned" not in injected


def test_build_context_refuses_symlinks(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    req = worktree / "requirements.txt"
    external = tmp_path / "secret.env"
    external.write_text("SECRET_TOKEN=xyz\n", encoding="utf-8")

    detected = DetectedEnvironment(
        template="requirements",
        python_version="3.12",
        dependency_files=("requirements.txt",),
    )

    try:
        req.symlink_to(external)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account cannot create symlinks; exercised on Linux CI")
        raise

    with pytest.raises(OSError, match="symlink"):
        snapshot_image("prj_123", worktree, detected)
