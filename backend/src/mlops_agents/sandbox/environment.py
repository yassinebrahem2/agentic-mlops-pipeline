"""Template choice, python version detection, and dependency file discovery."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from mlops_agents.sandbox.dependencies import read_dependency_files

DEFAULT_PYTHON_VERSION = "3.12"
VALID_PYTHON_VERSION_RE = re.compile(r"^\d+(\.\d+){0,2}$")


@dataclass(frozen=True)
class DetectedEnvironment:
    template: str
    python_version: str
    dependency_files: tuple[str, ...]


def _extract_requires_python_lower_bound(spec_str: str) -> str | None:
    """Parses requires-python specifier set and extracts the lower bound version."""
    try:
        spec_set = SpecifierSet(spec_str)
    except (InvalidSpecifier, ValueError):
        return None

    candidates: list[str] = []
    for spec in spec_set:
        if spec.operator in (">=", ">", "==", "===", "~="):
            ver = spec.version.removesuffix(".*")
            candidates.append(ver)

    if not candidates:
        return None

    try:
        return str(max(candidates, key=Version))
    except InvalidVersion:
        return None


def python_version_from_files(files: dict[str, bytes]) -> str:
    content = files.get(".python-version", b"").decode("utf-8", errors="replace").strip()
    if VALID_PYTHON_VERSION_RE.fullmatch(content):
        return content
    try:
        data = tomllib.loads(files.get("pyproject.toml", b"").decode("utf-8", errors="replace"))
    except tomllib.TOMLDecodeError:
        return DEFAULT_PYTHON_VERSION
    project = data.get("project")
    if isinstance(project, dict):
        requires_python = cast(dict[str, object], project).get("requires-python")
        if isinstance(requires_python, str):
            lower_bound = _extract_requires_python_lower_bound(requires_python.strip())
            if lower_bound and VALID_PYTHON_VERSION_RE.fullmatch(lower_bound):
                return lower_bound
    return DEFAULT_PYTHON_VERSION


def detect_python_version(path: Path) -> str:
    return python_version_from_files(read_dependency_files(path))


def template_from_files(files: dict[str, bytes]) -> tuple[str, tuple[str, ...]]:
    for name in ("environment.yml", "environment.yaml"):
        if name in files:
            return "micromamba", (name,)
    if "uv.lock" in files and "pyproject.toml" in files:
        return "uv", ("pyproject.toml", "uv.lock")
    if "pyproject.toml" in files:
        return "pyproject", ("pyproject.toml",)
    if "requirements.txt" in files:
        return "requirements", ("requirements.txt",)
    return "base", ()


def detect_template_and_dependencies(path: Path) -> tuple[str, tuple[str, ...]]:
    return template_from_files(read_dependency_files(path))


def environment_from_files(files: dict[str, bytes]) -> DetectedEnvironment:
    template, dep_files = template_from_files(files)
    return DetectedEnvironment(template, python_version_from_files(files), dep_files)


def detect_environment(path: Path) -> DetectedEnvironment:
    return environment_from_files(read_dependency_files(path))
