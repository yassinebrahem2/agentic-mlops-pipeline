from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

from mlops_agents.agents.tools.base import TaskRef, ToolContext, ToolFailure
from mlops_agents.agents.tools.files import is_secret

if TYPE_CHECKING:
    from mlops_agents.agents.tasks.contract import Tasks

__all__ = [
    "cap_diff",
    "compute_sha256",
    "get_current_task",
    "resolve_and_validate",
    "safe_write_bytes",
]

CHUNK_SIZE = 65536
MAX_DIFF_CHARS = 30_000


def compute_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def cap_diff(diff: str, max_chars: int = MAX_DIFF_CHARS) -> str:
    if len(diff) <= max_chars:
        return diff
    half = max_chars // 2
    cut = len(diff) - max_chars
    return diff[:half] + f"\n\n[... diff cut: {cut} characters omitted ...]\n\n" + diff[-half:]


def resolve_and_validate(worktree_root: Path, file_path: str) -> tuple[Path, str, Path]:
    if not file_path or not file_path.strip():
        raise ToolFailure("file_path cannot be empty.")

    resolved_worktree = worktree_root.resolve()
    candidate = Path(file_path)
    if not candidate.is_absolute():
        candidate = worktree_root / candidate

    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(resolved_worktree):
        raise ToolFailure(
            "That path is outside the worktree. Only files inside the task worktree can be modified."
        )

    rel = resolved.relative_to(resolved_worktree).as_posix()
    if not rel or rel == ".":
        raise ToolFailure("file_path must refer to a file, not the root directory.")

    if candidate.is_symlink():
        if not resolved.is_relative_to(resolved_worktree):
            raise ToolFailure(f"Symlink {candidate.name} escapes the worktree.")
        raise ToolFailure(f"Cannot write directly to symlink {candidate.name}.")

    cur = candidate.parent
    while cur != resolved_worktree and cur != cur.parent:
        if cur.is_symlink() and not cur.resolve().is_relative_to(resolved_worktree):
            raise ToolFailure(f"Path escapes worktree through symlink {cur.name}.")
        cur = cur.parent

    if is_secret(rel):
        raise ToolFailure(f"{rel} looks like a secret file, so it cannot be written.")

    return candidate, rel, resolved_worktree


async def get_current_task(ctx: ToolContext, tasks: Tasks) -> TaskRef:
    current = ctx.task or await tasks.current(ctx.project_id, ctx.session_id)
    if current is None:
        raise ToolFailure("Start a task first with StartTask.")
    return current


def safe_write_bytes(candidate: Path, resolved_worktree: Path, data: bytes) -> None:
    """Writes bytes to a candidate path safely:
    - refuses if candidate or any parent directory is a symlink;
    - ensures the resolved path is within resolved_worktree at open time;
    - opens with O_NOFOLLOW;
    - writes through the open file descriptor handle.
    """
    # 1. Refuse symlinks in parent components
    cur = candidate.parent
    while cur != resolved_worktree and cur != cur.parent:
        if cur.is_symlink():
            raise ToolFailure(f"Path contains symlinked directory {cur.name}.")
        cur = cur.parent

    # 2. Refuse symlink candidate
    if candidate.is_symlink():
        raise ToolFailure(f"Cannot write directly to symlink {candidate.name}.")

    # 3. Check resolved path before opening
    if not candidate.resolve(strict=False).is_relative_to(resolved_worktree):
        raise ToolFailure("File escaped worktree.")

    candidate.parent.mkdir(parents=True, exist_ok=True)

    # Re-verify parent components after mkdir
    cur = candidate.parent
    while cur != resolved_worktree and cur != cur.parent:
        if cur.is_symlink():
            raise ToolFailure(f"Path contains symlinked directory {cur.name}.")
        cur = cur.parent

    # 4. Open with no-follow
    o_nofollow: int = int(getattr(os, "O_NOFOLLOW", 0))
    o_binary: int = int(getattr(os, "O_BINARY", 0))
    flags: int = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | o_nofollow | o_binary

    try:
        fd = os.open(candidate, flags, 0o666)
    except OSError as exc:
        raise ToolFailure(f"Failed to open file safely: {exc.strerror or exc}") from None

    try:
        if candidate.is_symlink():
            raise ToolFailure(f"Cannot write directly to symlink {candidate.name}.")
        if not candidate.resolve(strict=False).is_relative_to(resolved_worktree):
            raise ToolFailure("File escaped worktree.")

        written = 0
        while written < len(data):
            n = os.write(fd, data[written:])
            if n == 0:
                break
            written += n
    finally:
        os.close(fd)
