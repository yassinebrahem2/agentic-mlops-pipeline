from __future__ import annotations

import asyncio
import fnmatch
import os
from pathlib import Path

from mlops_agents.agents.tools.base import ToolFailure
from mlops_agents.integrations.github.git import GitRunner

# File names that usually hold secrets. Matched case-insensitively against the base name.
# Known limitation (spec): a secret inside a tracked file with another name can still be read.
DENYLIST: tuple[str, ...] = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks",
    "*.kdbx", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".netrc", ".npmrc", ".pypirc",
    ".git-credentials", "credentials*.json", "*secret*.json",
)  # fmt: skip


def is_secret(rel_posix: str) -> bool:
    name = rel_posix.rsplit("/", 1)[-1].lower()
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in DENYLIST)


# git ls-files -s modes that are not regular files: symlinks (whose target may be outside the
# repository, such as ~/.aws/credentials) and submodules.
_NOT_REGULAR = frozenset({"120000", "160000"})


def _regular_files(ls_files_stage: str) -> list[str]:
    """Paths from `git ls-files -s -z` output whose mode is a regular file."""
    paths: list[str] = []
    for entry in ls_files_stage.split("\0"):
        meta, sep, path = entry.partition("\t")
        if sep and meta.split(" ", 1)[0] not in _NOT_REGULAR:
            paths.append(path)
    return paths


def norm_path(rel: str) -> str:
    return rel.casefold() if os.name == "nt" else rel


_norm = norm_path


class RepoFiles:
    """What an agent may see in a repository: regular files tracked by git, minus secrets,
    never outside the repository root (no `..`, no symlinks). One instance per turn."""

    def __init__(self, root: Path, git: GitRunner) -> None:
        self._root = root
        self._resolved_root: Path | None = None
        self._git = git
        self._tracked: list[str] | None = None
        self._lookup: dict[str, str] = {}

    @property
    def root(self) -> Path:
        return self._root

    async def tracked(self) -> list[str]:
        if self._tracked is None:
            result = await self._git.run(["ls-files", "-s", "-z"], cwd=self._root)
            files = _regular_files(result.stdout)
            self._tracked = sorted({p for p in files if not is_secret(p)})
            self._lookup = {_norm(p): p for p in self._tracked}
        return self._tracked

    async def under(self, prefix: str) -> list[str]:
        files = await self.tracked()
        if not prefix:
            return files
        wanted = _norm(prefix)
        return [f for f in files if _norm(f).startswith(wanted)]

    def _relative(self, user_path: str) -> str:
        """Blocking (resolves symlinks): call it through asyncio.to_thread."""
        if self._resolved_root is None:
            self._resolved_root = self._root.resolve()
        candidate = Path(user_path)
        if not candidate.is_absolute():
            candidate = self._root / candidate
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self._resolved_root):
            raise ToolFailure(
                "That path is outside the repository. Only files inside the project can be read."
            )
        return resolved.relative_to(self._resolved_root).as_posix()

    async def resolve_file(self, user_path: str) -> str:
        rel = await asyncio.to_thread(self._relative, user_path)
        if is_secret(rel):
            raise ToolFailure(f"{rel} looks like a secret file, so it cannot be read.")
        await self.tracked()
        actual = self._lookup.get(_norm(rel))
        if actual is None:
            raise ToolFailure(
                f"{rel} is not a file committed to the repository. Only committed files can be "
                "read; use Glob to find files."
            )
        return actual

    async def resolve_dir(self, user_path: str | None) -> str:
        if not user_path or user_path.strip() in (".", "./"):
            return ""
        rel = await asyncio.to_thread(self._relative, user_path)
        return "" if rel == "." else rel.rstrip("/") + "/"
