from __future__ import annotations

import re
import uuid
from pathlib import Path

__all__ = ["is_generated_temp_file", "make_temp_name"]

_GENERATED_TEMP_RE = re.compile(r"^\.tmp_.+_[0-9a-fA-F]{32}$")


def make_temp_name(filename: str) -> str:
    """Generates the temp file name used for atomic file writes (.tmp_<name>_<32 hex>)."""
    return f".tmp_{filename}_{uuid.uuid4().hex}"


def is_generated_temp_file(path_or_name: str | Path) -> bool:
    """Returns True if the path or file name matches the atomic write temp format (.tmp_<name>_<32 hex>)."""
    name = Path(path_or_name).name
    return bool(_GENERATED_TEMP_RE.match(name))
