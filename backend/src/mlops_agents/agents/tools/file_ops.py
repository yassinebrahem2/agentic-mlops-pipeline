from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from mlops_agents.agents.temp_files import make_temp_name
from mlops_agents.agents.tools.base import TaskRef, ToolContext, ToolFailure
from mlops_agents.agents.tools.files import is_secret

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from mlops_agents.agents.tasks.contract import Tasks
    from mlops_agents.sandbox.contract import Sandbox


@runtime_checkable
class HasPendingNote(Protocol):
    def pop_pending_note(self, project_id: str, task_id: str) -> str | None: ...


def pop_pending_note(tasks: object | None, project_id: str, task_id: str) -> str | None:
    if isinstance(tasks, HasPendingNote):
        return tasks.pop_pending_note(project_id, task_id)
    return None


__all__ = [
    "HasPendingNote",
    "compute_sha256",
    "get_current_task",
    "pause_task",
    "pop_pending_note",
    "resolve_and_validate",
    "safe_write_bytes",
]

CHUNK_SIZE = 65536


@asynccontextmanager
async def pause_task(
    sandbox: Sandbox | None, project_id: str, task_id: str
) -> AsyncGenerator[None, None]:
    """Freezes the task sandbox container during writes and commits so no host write races it."""
    if sandbox is not None:
        async with sandbox.paused(project_id, task_id):
            yield
    else:
        yield


def compute_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


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
    - writes to a new temporary file in the same directory (created exclusively);
    - drops O_TRUNC so link targets are never truncated;
    - replaces candidate atomically using os.replace, replacing any link without following it.
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

    # 4. Write to a new temp file in the same directory (created exclusively)
    o_binary: int = int(getattr(os, "O_BINARY", 0))
    flags: int = os.O_WRONLY | os.O_CREAT | os.O_EXCL | o_binary

    temp_path: Path | None = None
    try:
        temp_name = make_temp_name(candidate.name)
        temp_path = candidate.parent / temp_name
        try:
            fd = os.open(temp_path, flags, 0o666)
        except OSError as exc:
            raise ToolFailure(f"Failed to create temporary file: {exc.strerror or exc}") from None

        try:
            written = 0
            while written < len(data):
                n = os.write(fd, data[written:])
                if n == 0:
                    break
                written += n
        finally:
            os.close(fd)

        # Refuse if candidate is a symlink before replacing
        if candidate.is_symlink():
            raise ToolFailure(f"Cannot write directly to symlink {candidate.name}.")
        if not candidate.resolve(strict=False).is_relative_to(resolved_worktree):
            raise ToolFailure("File escaped worktree.")

        if candidate.exists():
            st_mode = candidate.stat().st_mode
            try:
                temp_path.chmod(st_mode)
            except OSError as exc:
                logger.warning(
                    "Failed to preserve file mode on %s: %s", temp_path, exc, exc_info=True
                )

        # Atomic replace replaces symlink without following or truncating target
        try:
            temp_path.replace(candidate)
        except OSError as exc:
            raise ToolFailure(
                f"Failed to replace {candidate.name}: {exc.strerror or exc}"
            ) from None
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError as exc:
                logger.warning(
                    "Failed to unlink temporary file %s: %s", temp_path, exc, exc_info=True
                )
