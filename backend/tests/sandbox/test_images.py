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
    load_template,
)


class FakeDocker(DockerRunner):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float | None] = []
        self.existing_images: set[str] = set()
        self.containers_by_image: dict[str, list[str]] = {}
        self.build_fail_output: str | None = None
        self.raise_timeout: bool = False

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
            if self.raise_timeout:
                raise TimeoutError("Docker build timed out")
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


async def test_build_timeout_caught_as_failed(tmp_path: Path) -> None:
    runner = FakeDocker()
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


def test_load_template_literal_substitution() -> None:
    content = load_template("base", "3.11")
    assert "ARG PYTHON_VERSION=3.11\n" in content

    # Injected shell commands / newlines must be rejected and fall back to default 3.12
    injected = load_template("base", "3.12\nRUN touch /tmp/pwned")
    assert "ARG PYTHON_VERSION=3.12\n" in injected
    assert "pwned" not in injected


def test_build_context_refuses_symlinks(tmp_path: Path) -> None:
    from unittest.mock import patch

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
    context_dir = tmp_path / "context"

    try:
        req.symlink_to(external)
        create_build_context(worktree, detected, context_dir)
        assert not (context_dir / "requirements.txt").exists()
    except OSError:
        req.write_text("SECRET_TOKEN=xyz\n", encoding="utf-8")
        with patch.object(Path, "is_symlink", return_value=True):
            create_build_context(worktree, detected, context_dir)
            assert not (context_dir / "requirements.txt").exists()


@pytest.mark.docker
@pytest.mark.parametrize(
    ("template", "files"),
    [
        ("base", {}),
        ("requirements", {"requirements.txt": "six\n"}),
        (
            "pyproject",
            {
                "pyproject.toml": '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = ["six"]\n'
            },
        ),
        (
            "uv",
            {
                "pyproject.toml": '[project]\nname = "demo"\nversion = "0.1.0"\n',
                "uv.lock": "version = 1\nrequires-python = '>=3.12'\n",
            },
        ),
        (
            "micromamba",
            {
                "environment.yml": "name: minenv\nchannels:\n  - conda-forge\ndependencies:\n  - python=3.12\n"
            },
        ),
    ],
)
async def test_real_docker_all_templates_build(
    tmp_path: Path, template: str, files: dict[str, str]
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    for fname, content in files.items():
        (worktree / fname).write_text(content, encoding="utf-8")

    detected = DetectedEnvironment(
        template=template,
        python_version="3.12",
        dependency_files=tuple(files.keys()),
    )
    runner = CliDockerRunner()
    manager = ImageManager(runner)
    project_id = f"prj_tmpl_{template}"

    status = await manager.build_image(project_id, worktree, detected)
    try:
        assert status.state == "ready", f"Build failed: {status.detail}"
        assert status.tag is not None
    finally:
        if status.tag:
            await runner.run(["image", "rm", status.tag])


@pytest.mark.docker
async def test_real_docker_environment_survives_login_shell(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("six\n", encoding="utf-8")
    detected = DetectedEnvironment(
        template="requirements",
        python_version="3.12",
        dependency_files=("requirements.txt",),
    )
    runner = CliDockerRunner()
    manager = ImageManager(runner)
    project_id = "prj_login_shell"

    status = await manager.build_image(project_id, worktree, detected)
    assert status.state == "ready", f"Build failed: {status.detail}"
    assert status.tag is not None
    tag = status.tag

    cname = "mlops-agents-test-login-shell"
    try:
        # Start container
        await runner.run(
            ["run", "-d", "--init", "--name", cname, "-w", "/workspace", tag, "sleep", "infinity"]
        )
        # Run bash -lc 'python -c "import six"'
        res = await runner.run(["exec", cname, "bash", "-lc", 'python -c "import six"'])
        assert res.exit_code == 0, f"Import in login shell failed: {res.stderr}"
    finally:
        await runner.run(["rm", "-f", cname])
        await runner.run(["image", "rm", tag])
