from __future__ import annotations

import asyncio
import os
import shutil
import stat
import string
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mlops_agents.errors import DomainError
from mlops_agents.workspace.models import FolderEntry, FolderListing


def rmtree(path: Path) -> None:
    def make_writable_and_retry(func: Callable[..., Any], target: str, exc: BaseException) -> None:
        del exc
        Path(target).chmod(
            stat.S_IWRITE
        )  # git marks pack files read-only; Windows refuses to delete them
        func(target)

    if path.exists():
        shutil.rmtree(path, onexc=make_writable_and_retry)


def user_path(text: str) -> Path:
    return Path(text.strip()).expanduser()


def drive_roots() -> list[str]:
    if os.name != "nt":
        return ["/"]
    # Probing a disconnected network drive can take seconds; this runs in a worker thread.
    return [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]


async def browse_folder(path_text: str | None) -> FolderListing:
    text = (path_text or "").strip()
    folder = await asyncio.to_thread(user_path, text) if text else Path.home()
    if not await asyncio.to_thread(folder.is_dir):
        raise DomainError(
            "folder_not_found", f"{folder} is not a folder on this machine.", status=404
        )

    def scan() -> list[FolderEntry]:
        found: list[FolderEntry] = []
        for child in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
            try:
                if child.is_dir():
                    found.append(
                        FolderEntry(
                            name=child.name,
                            path=str(child),
                            is_git_repo=(child / ".git").exists(),
                        )
                    )
            except OSError:
                continue
        return found

    try:
        entries = await asyncio.to_thread(scan)
    except PermissionError:
        raise DomainError(
            "folder_unreadable", f"The app is not allowed to read {folder}.", status=403
        ) from None
    except OSError as exc:  # a disconnected network path or a drive that is not ready
        reason = exc.strerror or type(exc).__name__
        raise DomainError(
            "folder_unreadable",
            f"{folder} could not be read ({reason}). Check the drive or network path and try again.",
            status=409,
        ) from None
    roots = await asyncio.to_thread(drive_roots)
    parent = str(folder.parent) if folder.parent != folder else None
    return FolderListing(path=str(folder), parent=parent, entries=entries, roots=roots)
