from __future__ import annotations

from pathlib import Path

import pytest

from mlops_agents.agents.tools.base import TaskRef
from mlops_agents.sandbox.containers import build_run_args, container_name, host_container_user
from mlops_agents.sandbox.docker import CliDockerRunner
from mlops_agents.sandbox.environment import DetectedEnvironment
from mlops_agents.sandbox.images import ImageManager


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


@pytest.mark.docker
@pytest.mark.parametrize(
    ("template", "files"),
    [
        ("base", {}),
        ("requirements", {"requirements.txt": "six\n"}),
        ("pyproject", {"pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1.0"\n'}),
        (
            "uv",
            {
                "pyproject.toml": (
                    '[project]\nname = "demo"\nversion = "0.1.0"\n'
                    'requires-python = ">=3.12"\ndependencies = ["six"]\n'
                ),
                "uv.lock": (
                    'version = 1\nrequires-python = ">=3.12"\n\n'
                    '[[package]]\nname = "demo"\nversion = "0.1.0"\n'
                    'source = { virtual = "." }\ndependencies = [{ name = "six" }]\n\n'
                    '[package.metadata]\nrequires-dist = [{ name = "six" }]\n\n'
                    '[[package]]\nname = "six"\nversion = "1.17.0"\n'
                    'source = { registry = "https://pypi.org/simple" }\n'
                    'wheels = [{ url = "https://files.pythonhosted.org/packages/b7/ce/149a00dd41f10bc29e5921b496af8b574d8413afcd5e30dfa0ed46c2cc5e/six-1.17.0-py2.py3-none-any.whl", hash = "sha256:4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274", size = 11050 }]\n'
                ),
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

    task = TaskRef("test-login-shell", "Login shell", "branch", worktree, "active")
    cname = container_name(task.id)
    try:
        # Start container
        await runner.run(build_run_args(project_id, task, tag, user=host_container_user()))
        # Run bash -lc 'python -c "import six"'
        res = await runner.run(["exec", cname, "bash", "-lc", 'python -c "import six"'])
        assert res.exit_code == 0, f"Import in login shell failed: {res.stderr}"
    finally:
        await runner.run(["rm", "-f", cname])
        await runner.run(["image", "rm", tag])
