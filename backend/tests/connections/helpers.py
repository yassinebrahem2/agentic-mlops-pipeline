"""Folder links for the connection tests: symlinks where the OS allows them, Windows junctions."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def make_junction(target: Path, link: Path) -> bool:
    """A Windows junction at `link` pointing to `target`; False off Windows or when refused."""
    if os.name != "nt":
        return False
    res = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
    )
    return res.returncode == 0


def make_link_or_junction(target: Path, link: Path) -> bool:
    """A directory symlink, or a junction on Windows when symlinks are not allowed."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except OSError:
        return make_junction(target, link)
