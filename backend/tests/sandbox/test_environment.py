from __future__ import annotations

from pathlib import Path

import pytest

from mlops_agents.sandbox.environment import (
    DEFAULT_PYTHON_VERSION,
    DetectedEnvironment,
    detect_environment,
    detect_python_version,
)


def test_python_version_default(tmp_path: Path) -> None:
    assert detect_python_version(tmp_path) == DEFAULT_PYTHON_VERSION
    assert detect_python_version(tmp_path) == "3.12"


def test_python_version_from_python_version_file(tmp_path: Path) -> None:
    (tmp_path / ".python-version").write_text("3.11\n", encoding="utf-8")
    assert detect_python_version(tmp_path) == "3.11"


def test_python_version_from_pyproject_toml(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nrequires-python = ">=3.11"\n',
        encoding="utf-8",
    )
    assert detect_python_version(tmp_path) == "3.11"


def test_python_version_precedence(tmp_path: Path) -> None:
    (tmp_path / ".python-version").write_text("3.10\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nrequires-python = ">=3.11"\n',
        encoding="utf-8",
    )
    assert detect_python_version(tmp_path) == "3.10"


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (">=3.11", "3.11"),
        (">=3.12, <4.0", "3.12"),
        (">= 3.10.4", "3.10.4"),
        ("==3.11.*", "3.11"),
        ("~=3.9.0", "3.9.0"),
        ("<3.13,>=3.10", "3.10"),
        (">=3.8, <3.12, >=3.10", "3.10"),
    ],
)
def test_python_version_requires_python_formats(tmp_path: Path, spec: str, expected: str) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "demo"\nrequires-python = "{spec}"\n',
        encoding="utf-8",
    )
    assert detect_python_version(tmp_path) == expected


def test_detect_environment_conda_yaml(tmp_path: Path) -> None:
    (tmp_path / "environment.yml").write_text("name: test\n", encoding="utf-8")
    env = detect_environment(tmp_path)
    assert env == DetectedEnvironment(
        template="micromamba",
        python_version="3.12",
        dependency_files=("environment.yml",),
    )


def test_detect_environment_conda_alternate_ext(tmp_path: Path) -> None:
    (tmp_path / "environment.yaml").write_text("name: test\n", encoding="utf-8")
    env = detect_environment(tmp_path)
    assert env == DetectedEnvironment(
        template="micromamba",
        python_version="3.12",
        dependency_files=("environment.yaml",),
    )


def test_detect_environment_uv(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    env = detect_environment(tmp_path)
    assert env == DetectedEnvironment(
        template="uv",
        python_version="3.12",
        dependency_files=("pyproject.toml", "uv.lock"),
    )


def test_detect_environment_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nrequires-python = ">=3.11"\n',
        encoding="utf-8",
    )
    env = detect_environment(tmp_path)
    assert env == DetectedEnvironment(
        template="pyproject",
        python_version="3.11",
        dependency_files=("pyproject.toml",),
    )


def test_detect_environment_requirements(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pandas>=2.0\n", encoding="utf-8")
    env = detect_environment(tmp_path)
    assert env == DetectedEnvironment(
        template="requirements",
        python_version="3.12",
        dependency_files=("requirements.txt",),
    )


def test_detect_environment_base(tmp_path: Path) -> None:
    env = detect_environment(tmp_path)
    assert env == DetectedEnvironment(
        template="base",
        python_version="3.12",
        dependency_files=(),
    )


def test_detect_environment_precedence_order(tmp_path: Path) -> None:
    # If all files exist, micromamba (environment.yml) wins
    (tmp_path / "environment.yml").write_text("name: test\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("pandas\n", encoding="utf-8")

    env = detect_environment(tmp_path)
    assert env.template == "micromamba"
    assert env.dependency_files == ("environment.yml",)

    # Remove environment.yml -> uv wins
    (tmp_path / "environment.yml").unlink()
    env = detect_environment(tmp_path)
    assert env.template == "uv"
    assert env.dependency_files == ("pyproject.toml", "uv.lock")

    # Remove uv.lock -> pyproject wins
    (tmp_path / "uv.lock").unlink()
    env = detect_environment(tmp_path)
    assert env.template == "pyproject"
    assert env.dependency_files == ("pyproject.toml",)

    # Remove pyproject.toml -> requirements wins
    (tmp_path / "pyproject.toml").unlink()
    env = detect_environment(tmp_path)
    assert env.template == "requirements"
    assert env.dependency_files == ("requirements.txt",)

    # Remove requirements.txt -> base
    (tmp_path / "requirements.txt").unlink()
    env = detect_environment(tmp_path)
    assert env.template == "base"
    assert env.dependency_files == ()


def test_python_version_invalid_injection_rejected(tmp_path: Path) -> None:
    (tmp_path / ".python-version").write_text("3.12\nRUN touch /tmp/pwned\n", encoding="utf-8")
    assert detect_python_version(tmp_path) == DEFAULT_PYTHON_VERSION


def test_symlinked_dependency_refused(tmp_path: Path) -> None:
    req = tmp_path / "requirements.txt"
    external = tmp_path.parent / "secret.txt"
    external.write_text("secret_key=xyz", encoding="utf-8")

    try:
        req.symlink_to(external)
        env = detect_environment(tmp_path)
        assert env.template == "base"
        assert env.dependency_files == ()
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account cannot create symlinks; exercised on Linux CI")
        raise


def test_symlinked_python_version_refused(tmp_path: Path) -> None:
    pyver = tmp_path / ".python-version"
    external = tmp_path.parent / "secret_ver.txt"
    external.write_text("3.11", encoding="utf-8")

    try:
        pyver.symlink_to(external)
        ver = detect_python_version(tmp_path)
        assert ver == DEFAULT_PYTHON_VERSION
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account cannot create symlinks; exercised on Linux CI")
        raise
