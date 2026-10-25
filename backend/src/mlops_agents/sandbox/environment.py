"""Template choice, python version detection, and dependency file discovery."""

from __future__ import annotations

import contextlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PYTHON_VERSION = "3.12"

_PYTHON_SPEC_PATTERN = re.compile(r"(?:>=|==|~=|>)?\s*(\d+(?:\.\d+)*)")


@dataclass(frozen=True)
class DetectedEnvironment:
    template: str
    python_version: str
    dependency_files: tuple[str, ...]


def detect_python_version(path: Path) -> str:
    """Extracts python version: .python-version, else lower bound of requires-python, else 3.12."""
    py_ver_file = path / ".python-version"
    if py_ver_file.is_file():
        content = py_ver_file.read_text(encoding="utf-8", errors="replace").strip()
        if content:
            return content

    pyproject_file = path / "pyproject.toml"
    if pyproject_file.is_file():
        with contextlib.suppress(tomllib.TOMLDecodeError, OSError, KeyError, TypeError):
            data = tomllib.loads(pyproject_file.read_text(encoding="utf-8", errors="replace"))
            requires_python = data.get("project", {}).get("requires-python")
            if isinstance(requires_python, str) and requires_python.strip():
                match = _PYTHON_SPEC_PATTERN.search(requires_python.strip())
                if match and match.group(1):
                    return match.group(1)

    return DEFAULT_PYTHON_VERSION


def detect_template_and_dependencies(path: Path) -> tuple[str, tuple[str, ...]]:
    """Determines template and dependency files in strict priority order."""
    if (path / "environment.yml").is_file():
        return "micromamba", ("environment.yml",)
    if (path / "environment.yaml").is_file():
        return "micromamba", ("environment.yaml",)
    if (path / "uv.lock").is_file() and (path / "pyproject.toml").is_file():
        return "uv", ("pyproject.toml", "uv.lock")
    if (path / "pyproject.toml").is_file():
        return "pyproject", ("pyproject.toml",)
    if (path / "requirements.txt").is_file():
        return "requirements", ("requirements.txt",)
    return "base", ()


def detect_environment(path: Path) -> DetectedEnvironment:
    """Detects environment template, python version, and dependency files for a path."""
    template, dep_files = detect_template_and_dependencies(path)
    py_ver = detect_python_version(path)
    return DetectedEnvironment(
        template=template,
        python_version=py_ver,
        dependency_files=dep_files,
    )
