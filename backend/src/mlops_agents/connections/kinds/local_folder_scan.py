"""File-system scanning, table discovery, data version hashing and findings walk for local folders."""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from mlops_agents.agents.tools.files import is_secret
from mlops_agents.connections.contract import Finding, Findings, TableRef
from mlops_agents.errors import DomainError

MAX_TABLE_FILES: Final = 10_000
MAX_SCAN_ENTRIES: Final = 50_000
MAX_SECRET_FINDINGS: Final = 100

TableFormat = Literal["csv", "parquet", "json", "jsonl"]


@dataclass
class _CandidateTable:
    type: str
    entry_name: str
    default_name: str
    format: TableFormat
    files: list[str]


SUPPORTED_EXTENSIONS: Final[dict[str, TableFormat]] = {
    ".csv": "csv",
    ".parquet": "parquet",
    ".json": "json",
    ".jsonl": "jsonl",
}


def is_link(entry: os.DirEntry[str]) -> bool:
    """True if entry is a symlink or Windows junction."""
    return entry.is_symlink() or bool(getattr(entry, "is_junction", None) and entry.is_junction())


def scan_tables_and_unsupported(
    folder: Path,
) -> tuple[list[TableRef], list[Finding]]:
    """Scans the top level of folder for supported table files and subfolders."""
    candidate_tables: list[_CandidateTable] = []
    unsupported: list[Finding] = []

    try:
        with os.scandir(folder) as it:
            entries = sorted(it, key=lambda e: e.name.casefold())
    except (PermissionError, OSError) as exc:
        reason = getattr(exc, "strerror", None) or type(exc).__name__
        raise DomainError(
            "connection_invalid",
            f"{folder} could not be read ({reason}).",
            status=400,
        ) from None

    for entry in entries:
        if is_link(entry):
            unsupported.append(Finding(path=entry.name, reason="a link"))
            continue

        if entry.is_file(follow_symlinks=False):
            ext = Path(entry.name).suffix.lower()
            if ext in SUPPORTED_EXTENSIONS:
                candidate_tables.append(
                    _CandidateTable(
                        type="file",
                        entry_name=entry.name,
                        default_name=Path(entry.name).stem.lower(),
                        format=SUPPORTED_EXTENSIONS[ext],
                        files=[entry.name],
                    )
                )
            else:
                unsupported.append(Finding(path=entry.name, reason="not a supported format"))
            continue

        if entry.is_dir(follow_symlinks=False):
            try:
                with os.scandir(entry.path) as child_it:
                    child_entries = list(child_it)
            except (PermissionError, OSError):
                unsupported.append(Finding(path=entry.name, reason="not a supported format"))
                continue

            non_dot = [c for c in child_entries if not c.name.startswith(".")]
            if not non_dot:
                unsupported.append(Finding(path=entry.name, reason="not a supported format"))
                continue

            if any(is_link(c) for c in non_dot):
                unsupported.append(Finding(path=entry.name, reason="a link"))
                continue

            if len(non_dot) > MAX_TABLE_FILES:
                unsupported.append(Finding(path=entry.name, reason="too many files"))
                continue

            if any(not c.is_file(follow_symlinks=False) for c in non_dot):
                unsupported.append(Finding(path=entry.name, reason="not a supported format"))
                continue

            exts = {Path(c.name).suffix.lower() for c in non_dot}
            if len(exts) > 1:
                unsupported.append(Finding(path=entry.name, reason="mixed formats"))
                continue

            (the_ext,) = exts
            if the_ext not in SUPPORTED_EXTENSIONS:
                unsupported.append(Finding(path=entry.name, reason="not a supported format"))
                continue

            rel_files = sorted(f"{entry.name}/{c.name}" for c in non_dot)
            candidate_tables.append(
                _CandidateTable(
                    type="folder",
                    entry_name=entry.name,
                    default_name=entry.name,
                    format=SUPPORTED_EXTENSIONS[the_ext],
                    files=rel_files,
                )
            )

    # Name collisions: when two tables would share a default name, the files among them use full file name.
    default_names = [c.default_name for c in candidate_tables]
    counts = Counter(default_names)

    tables: list[TableRef] = []
    for c in candidate_tables:
        final_name = (
            c.entry_name if counts[c.default_name] > 1 and c.type == "file" else c.default_name
        )
        tables.append(
            TableRef(
                name=final_name,
                files=c.files,
                format=c.format,
            )
        )

    tables.sort(key=lambda t: t.name)
    unsupported.sort(key=lambda u: u.path)
    return tables, unsupported


def compute_data_version(folder: Path, table: TableRef) -> str:
    """Sha256 hex (first 16 chars) of relpath\\0size\\0mtime_ns lines."""
    lines: list[str] = []
    for rel in sorted(table.files):
        file_path = folder / rel
        try:
            st = file_path.stat(follow_symlinks=False)
        except (FileNotFoundError, OSError):
            raise DomainError(
                "connection_invalid",
                f"File vanished or unreadable: {rel}",
                status=400,
            ) from None
        lines.append(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}")

    data = "\n".join(lines).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


def latest_data_version(folder: Path, tables: list[TableRef]) -> str | None:
    """The data version of the table whose newest file has the latest modified time."""
    if not tables:
        return None

    latest_mtime = -1
    newest_table: TableRef | None = None

    for table in tables:
        table_max_mtime = -1
        for rel in table.files:
            try:
                st = (folder / rel).stat(follow_symlinks=False)
                if st.st_mtime_ns > table_max_mtime:
                    table_max_mtime = st.st_mtime_ns
            except OSError:
                continue
        if table_max_mtime > latest_mtime:
            latest_mtime = table_max_mtime
            newest_table = table

    if newest_table is None:
        return None
    return compute_data_version(folder, newest_table)


def walk_findings(folder: Path, unsupported: list[Finding]) -> Findings:
    """Walks the folder for secret files matching denylist, combining with unsupported files."""
    secret_findings: list[Finding] = []
    truncated = False
    entries_count = 0

    for root, dirs, files in os.walk(folder, followlinks=False):
        dirs.sort()
        files.sort()

        entries_count += len(dirs) + len(files)

        for f in files:
            full_path = Path(root) / f
            rel_posix = full_path.relative_to(folder).as_posix()
            if is_secret(rel_posix):
                secret_findings.append(Finding(path=rel_posix, reason="matches a secret file name"))
                if len(secret_findings) >= MAX_SECRET_FINDINGS:
                    truncated = True
                    break

        if truncated or entries_count >= MAX_SCAN_ENTRIES:
            truncated = True
            break

    secret_findings.sort(key=lambda s: s.path)
    return Findings(
        unsupported_files=unsupported,
        secret_files=secret_findings,
        truncated=truncated,
    )
