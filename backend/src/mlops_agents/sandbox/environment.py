"""Template choice, python version detection, and dependency file discovery."""

from __future__ import annotations

import contextlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

DEFAULT_PYTHON_VERSION = "3.12"
VALID_PYTHON_VERSION_RE = re.compile(r"^\d+(\.\d+){0,2}$")


@dataclass(frozen=True)
class DetectedEnvironment:
    template: str
    python_version: str
    dependency_files: tuple[str, ...]


def is_safe_regular_file(root: Path, filename: str) -> bool:
    """Verifies that filename is a regular file within root, refusing symlinks and escapes."""
    candidate = root / filename
    try:
        # Refuse symlinks directly
        if candidate.is_symlink():
            return False
        if not candidate.is_file():
            return False

        root_resolved = root.resolve(strict=True)
        file_resolved = candidate.resolve(strict=True)

        # Refuse symlink resolved target or non-file
        if file_resolved.is_symlink() or not file_resolved.is_file():
            return False

        # Ensure resolved path is strictly inside the root
        return file_resolved.is_relative_to(root_resolved)
    except (OSError, ValueError):
        return False


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
    except Exception:
        return candidates[0]


def detect_python_version(path: Path) -> str:
    """Extracts python version: .python-version, else lower bound of requires-python, else 3.12."""
    if is_safe_regular_file(path, ".python-version"):
        content = (path / ".python-version").read_text(encoding="utf-8", errors="replace").strip()
        if content and VALID_PYTHON_VERSION_RE.match(content):
            return content

    if is_safe_regular_file(path, "pyproject.toml"):
        with contextlib.suppress(tomllib.TOMLDecodeError, OSError, KeyError, TypeError):
            data = tomllib.loads(
                (path / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
            )
            requires_python = data.get("project", {}).get("requires-python")
            if isinstance(requires_python, str) and requires_python.strip():
                lower_bound = _extract_requires_python_lower_bound(requires_python.strip())
                if lower_bound and VALID_PYTHON_VERSION_RE.match(lower_bound):
                    return lower_bound

    return DEFAULT_PYTHON_VERSION


def detect_template_and_dependencies(path: Path) -> tuple[str, tuple[str, ...]]:
    """Determines template and dependency files in strict priority order, refusing symlinks."""
    if is_safe_regular_file(path, "environment.yml"):
        return "micromamba", ("environment.yml",)
    if is_safe_regular_file(path, "environment.yaml"):
        return "micromamba", ("environment.yaml",)
    if is_safe_regular_file(path, "uv.lock") and is_safe_regular_file(path, "pyproject.toml"):
        return "uv", ("pyproject.toml", "uv.lock")
    if is_safe_regular_file(path, "pyproject.toml"):
        return "pyproject", ("pyproject.toml",)
    if is_safe_regular_file(path, "requirements.txt"):
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
