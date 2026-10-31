"""File source reader for monitoring results (I22)."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from mlops_agents.connections.contract import LOCAL_FOLDER_KIND, Connections
from mlops_agents.credentials.scrub import scrub_url_secrets
from mlops_agents.monitoring.results.contract import ResultRecord, parse_source_id
from mlops_agents.monitoring.results.mapping import split_csv_records
from mlops_agents.tracking.contract import EvaluationRecord

logger = logging.getLogger(__name__)


@dataclass
class SourceReadOutcome:
    records: list[tuple[str, ResultRecord]]
    cursor: dict[str, Any]
    consecutive_failures: int
    last_error: str | None
    evaluations: list[EvaluationRecord] = field(default_factory=list[EvaluationRecord])


@dataclass
class RawChunk:
    rel_posix: str
    content: str
    start_offset: int
    new_offset: int
    size: int
    mtime: float
    csv_header: list[str] | None = None


@dataclass
class RawReadOutcome:
    chunks: list[RawChunk]
    cursor: dict[str, Any]
    last_error: str | None = None


def _unchanged(
    files_cursor: dict[str, dict[str, Any]], rel_posix: str, size: int, mtime: float
) -> bool:
    """Whether the file has the size and mtime of the previous read. A file seen for the
    first time has no previous read, so it is not unchanged."""
    prev = files_cursor.get(rel_posix)
    return prev is not None and prev.get("size") == size and prev.get("mtime") == mtime


def _consumable_length(chunk: bytes, unchanged: bool) -> int:
    """How many bytes of a chunk read from the cursor can be consumed now.

    Up to the last newline: bytes after it may be a record the writer is still writing. Once
    the file is unchanged since the previous read the writer has stopped, so that tail is a
    complete record (or a broken one, which the parser reports) and is consumed too."""
    if unchanged:
        return len(chunk)
    return chunk.rfind(b"\n") + 1


def _read_files_sync(
    root: Path,
    pattern: str,
    secret_paths: set[str],
    cursor: dict[str, Any],
    consecutive_failures: int = 0,
) -> SourceReadOutcome:
    """Synchronous file scanner and reader for local folder paths."""
    files_cursor: dict[str, dict[str, Any]] = dict(cursor.get("files", {}))
    last_error: str | None = None
    records: list[tuple[str, ResultRecord]] = []
    evaluations: list[EvaluationRecord] = []
    candidate_files: list[tuple[str, Path]] = []

    try:
        matches = sorted(root.glob(pattern))
    except OSError as exc:
        return SourceReadOutcome(
            records=[],
            cursor=cursor,
            consecutive_failures=consecutive_failures + 1,
            last_error=scrub_url_secrets(str(exc)),
        )

    for match in matches:
        try:
            resolved = match.resolve()
            if not resolved.is_file():
                continue
        except OSError as exc:
            consecutive_failures += 1
            last_error = scrub_url_secrets(str(exc))
            continue

        try:
            if not resolved.is_relative_to(root):
                continue
            rel_posix = resolved.relative_to(root).as_posix()
        except ValueError:
            continue

        if rel_posix in secret_paths:
            continue

        candidate_files.append((rel_posix, resolved))

    for rel_posix, resolved_path in candidate_files:
        try:
            stat = resolved_path.stat()
        except OSError as exc:
            consecutive_failures += 1
            last_error = scrub_url_secrets(str(exc))
            continue

        cur_size = stat.st_size
        cur_mtime = stat.st_mtime
        prev = files_cursor.get(rel_posix, {})
        prev_offset = prev.get("offset", 0)
        prev_size = prev.get("size", 0)
        prev_mtime = prev.get("mtime", 0.0)

        if (
            cur_size < prev_offset
            or cur_size < prev_size
            or (cur_mtime != prev_mtime and cur_size < prev_offset)
        ):
            offset = 0
        else:
            offset = prev_offset

        if offset >= cur_size:
            files_cursor[rel_posix] = {
                "size": cur_size,
                "mtime": cur_mtime,
                "offset": offset,
            }
            continue

        try:
            with resolved_path.open("rb") as f:
                f.seek(offset)
                chunk = f.read(cur_size - offset)
        except OSError as exc:
            consecutive_failures += 1
            last_error = scrub_url_secrets(str(exc))
            continue

        consumable = _consumable_length(
            chunk, _unchanged(files_cursor, rel_posix, cur_size, cur_mtime)
        )
        if consumable == 0:
            files_cursor[rel_posix] = {
                "size": cur_size,
                "mtime": cur_mtime,
                "offset": offset,
            }
            continue

        chunk_to_consume = chunk[:consumable]
        new_offset = offset + consumable

        current_line_start = offset
        lines = chunk_to_consume.splitlines(keepends=True)
        for raw_line_bytes in lines:
            line_offset = current_line_start
            current_line_start += len(raw_line_bytes)

            line_str = raw_line_bytes.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue

            try:
                parsed_json = json.loads(line_str)
                if not isinstance(parsed_json, dict):
                    raise ValueError("JSON line is not an object")

                raw_dict = cast(dict[str, Any], parsed_json)

                # Check if this is an evaluation copy line
                if (
                    "check_id" not in raw_dict
                    and "schema_version" in raw_dict
                    and "evaluation_commit" in raw_dict
                ):
                    model = raw_dict.get("model")
                    version = raw_dict.get("version")
                    if not model or not version:
                        raise ValueError("Evaluation record missing model or version")

                    run_key = str(raw_dict.get("run_key") or "")
                    schema_version = int(raw_dict["schema_version"])
                    eval_rec = EvaluationRecord(
                        source_id="",
                        model=str(model),
                        version=str(version),
                        run_key=run_key,
                        schema_version=schema_version,
                        payload=raw_dict,
                        fetched_at=datetime.now(UTC),
                    )
                    evaluations.append(eval_rec)
                    continue

                custom_key: Any = raw_dict.pop("source_key", None) or raw_dict.pop("key", None)
                source_key = (
                    str(custom_key) if custom_key is not None else f"{rel_posix}:{line_offset}"
                )

                origin_val = raw_dict.get("origin")
                if origin_val != "agent":
                    raise ValueError(f"origin must be 'agent', found {origin_val!r}")

                rec = ResultRecord.model_validate(raw_dict)
                records.append((source_key, rec))
            except Exception as exc:
                consecutive_failures += 1
                last_error = scrub_url_secrets(str(exc))
                logger.warning(
                    "Invalid result line in %s at byte offset %d: %s",
                    rel_posix,
                    line_offset,
                    last_error,
                )

        files_cursor[rel_posix] = {
            "size": cur_size,
            "mtime": cur_mtime,
            "offset": new_offset,
        }

    return SourceReadOutcome(
        records=records,
        cursor={"files": files_cursor},
        consecutive_failures=consecutive_failures,
        last_error=last_error,
        evaluations=evaluations,
    )


class FileSourceReader:
    """Reads standard results from a local_folder connection path pattern."""

    def __init__(self, connections: Connections) -> None:
        self._connections = connections

    async def read(
        self,
        project_id: str,
        source_id: str,
        cursor: dict[str, Any],
        *,
        consecutive_failures: int = 0,
    ) -> SourceReadOutcome:
        """Reads new result records from files matching source_id pattern."""
        kind, pattern, connection_name = parse_source_id(source_id)
        if kind != "file" or connection_name is None:
            return SourceReadOutcome(
                records=[],
                cursor=cursor,
                consecutive_failures=consecutive_failures + 1,
                last_error=f"Invalid file source_id: {source_id}",
            )

        p = Path(pattern)
        if p.is_absolute() or pattern.startswith(("/", "\\")) or ".." in p.parts:
            return SourceReadOutcome(
                records=[],
                cursor=cursor,
                consecutive_failures=consecutive_failures + 1,
                last_error="Pattern cannot be absolute or navigate with '..'",
            )

        connections_list = await self._connections.list(project_id)
        matching_conn = next((c for c in connections_list if c.name == connection_name), None)
        if matching_conn is None:
            return SourceReadOutcome(
                records=[],
                cursor=cursor,
                consecutive_failures=consecutive_failures + 1,
                last_error=f"Connection {connection_name!r} not found",
            )

        if matching_conn.kind != LOCAL_FOLDER_KIND:
            return SourceReadOutcome(
                records=[],
                cursor=cursor,
                consecutive_failures=consecutive_failures + 1,
                last_error=f"Connection {connection_name!r} is not a {LOCAL_FOLDER_KIND}",
            )

        path_setting = matching_conn.settings.get("path", "")

        def _resolve_root() -> Path | None:
            r = Path(path_setting).resolve()
            return r if r.exists() and r.is_dir() else None

        root = await asyncio.to_thread(_resolve_root)
        if root is None:
            return SourceReadOutcome(
                records=[],
                cursor=cursor,
                consecutive_failures=consecutive_failures + 1,
                last_error=f"Connection folder {path_setting!r} does not exist",
            )

        findings = await self._connections.inspect(
            project_id, matching_conn.kind, matching_conn.settings
        )
        secret_paths = {f.path for f in findings.secret_files}

        try:
            return await asyncio.to_thread(
                _read_files_sync, root, pattern, secret_paths, cursor, consecutive_failures
            )
        except Exception as exc:
            return SourceReadOutcome(
                records=[],
                cursor=cursor,
                consecutive_failures=consecutive_failures + 1,
                last_error=scrub_url_secrets(str(exc)),
            )

    async def read_raw(
        self,
        project_id: str,
        source_id: str,
        cursor: dict[str, Any],
        *,
        format: str | None = None,
    ) -> RawReadOutcome:
        """Reads raw file contents from files matching source_id pattern."""
        kind, pattern, connection_name = parse_source_id(source_id)
        if kind != "file" or connection_name is None:
            return RawReadOutcome(
                chunks=[],
                cursor=cursor,
                last_error=f"Invalid file source_id: {source_id}",
            )

        p = Path(pattern)
        if p.is_absolute() or pattern.startswith(("/", "\\")) or ".." in p.parts:
            return RawReadOutcome(
                chunks=[],
                cursor=cursor,
                last_error="Pattern cannot be absolute or navigate with '..'",
            )

        connections_list = await self._connections.list(project_id)
        matching_conn = next((c for c in connections_list if c.name == connection_name), None)
        if matching_conn is None:
            return RawReadOutcome(
                chunks=[],
                cursor=cursor,
                last_error=f"Connection {connection_name!r} not found",
            )

        if matching_conn.kind != LOCAL_FOLDER_KIND:
            return RawReadOutcome(
                chunks=[],
                cursor=cursor,
                last_error=f"Connection {connection_name!r} is not a {LOCAL_FOLDER_KIND}",
            )

        path_setting = matching_conn.settings.get("path", "")

        def _resolve_root() -> Path | None:
            r = Path(path_setting).resolve()
            return r if r.exists() and r.is_dir() else None

        root = await asyncio.to_thread(_resolve_root)
        if root is None:
            return RawReadOutcome(
                chunks=[],
                cursor=cursor,
                last_error=f"Connection folder {path_setting!r} does not exist",
            )

        findings = await self._connections.inspect(
            project_id, matching_conn.kind, matching_conn.settings
        )
        secret_paths = {f.path for f in findings.secret_files}

        try:
            return await asyncio.to_thread(
                _read_raw_files_sync, root, pattern, secret_paths, cursor, format
            )
        except Exception as exc:
            return RawReadOutcome(
                chunks=[],
                cursor=cursor,
                last_error=scrub_url_secrets(str(exc)),
            )


def _byte_end(data: bytes, text: str, end: int) -> int:
    """Byte position in `data` of `end`, a text index just past a newline of `text`.

    Counts newlines instead of re-encoding the text: bytes that are not UTF-8 decode to
    U+FFFD and re-encode to a different length, but each "\\n" is exactly one 0x0A byte."""
    position = 0
    for _ in range(text.count("\n", 0, end)):
        position = data.index(b"\n", position) + 1
    return position


def _read_raw_files_sync(
    root: Path,
    pattern: str,
    secret_paths: set[str],
    cursor: dict[str, Any],
    format: str | None = None,
) -> RawReadOutcome:
    files_cursor: dict[str, dict[str, Any]] = dict(cursor.get("files", {}))
    chunks: list[RawChunk] = []
    candidate_files: list[tuple[str, Path]] = []
    last_error: str | None = None

    try:
        matches = sorted(root.glob(pattern))
    except OSError as exc:
        return RawReadOutcome(
            chunks=[],
            cursor=cursor,
            last_error=scrub_url_secrets(str(exc)),
        )

    for match in matches:
        try:
            resolved = match.resolve()
            if not resolved.is_file():
                continue
        except OSError as exc:
            last_error = scrub_url_secrets(str(exc))
            continue

        try:
            if not resolved.is_relative_to(root):
                continue
            rel_posix = resolved.relative_to(root).as_posix()
        except ValueError:
            continue

        if rel_posix in secret_paths:
            continue

        candidate_files.append((rel_posix, resolved))

    for rel_posix, resolved_path in candidate_files:
        try:
            stat = resolved_path.stat()
        except OSError as exc:
            last_error = scrub_url_secrets(str(exc))
            continue

        cur_size = stat.st_size
        cur_mtime = stat.st_mtime
        prev = files_cursor.get(rel_posix, {})
        prev_offset = prev.get("offset", 0)
        prev_size = prev.get("size", 0)
        prev_mtime = prev.get("mtime", 0.0)
        saved_header: list[str] | None = prev.get("csv_header")

        if format == "json":
            if cur_size == prev_size and cur_mtime == prev_mtime:
                continue

            try:
                with resolved_path.open("rb") as f:
                    chunk = f.read(cur_size)
            except OSError as exc:
                last_error = scrub_url_secrets(str(exc))
                continue

            raw_text = chunk.decode("utf-8", errors="replace")
            chunks.append(
                RawChunk(
                    rel_posix=rel_posix,
                    content=raw_text,
                    start_offset=0,
                    new_offset=cur_size,
                    size=cur_size,
                    mtime=cur_mtime,
                )
            )
            files_cursor[rel_posix] = {
                "size": cur_size,
                "mtime": cur_mtime,
                "offset": cur_size,
            }
            continue

        if (
            cur_size < prev_offset
            or cur_size < prev_size
            or (cur_mtime != prev_mtime and cur_size < prev_offset)
        ):
            offset = 0
            saved_header = None
        else:
            offset = prev_offset

        if offset >= cur_size:
            files_cursor[rel_posix] = {
                "size": cur_size,
                "mtime": cur_mtime,
                "offset": offset,
                "csv_header": saved_header,
            }
            continue

        try:
            with resolved_path.open("rb") as f:
                f.seek(offset)
                chunk = f.read(cur_size - offset)
        except OSError as exc:
            last_error = scrub_url_secrets(str(exc))
            continue

        unchanged = _unchanged(files_cursor, rel_posix, cur_size, cur_mtime)
        consumable = _consumable_length(chunk, unchanged)
        if consumable == 0:
            files_cursor[rel_posix] = {
                "size": cur_size,
                "mtime": cur_mtime,
                "offset": offset,
                "csv_header": saved_header,
            }
            continue

        chunk_to_consume = chunk[:consumable]
        new_offset = offset + consumable
        raw_text = chunk_to_consume.decode("utf-8", errors="replace")

        if format == "csv":
            split = split_csv_records(raw_text, offset)
            # An unfinished quoted record may still be completed while the file changes; once
            # it is unchanged the record goes to the parser whole, which reports it as broken.
            if not unchanged:
                if split.end == 0:
                    files_cursor[rel_posix] = {
                        "size": cur_size,
                        "mtime": cur_mtime,
                        "offset": offset,
                        "csv_header": saved_header,
                    }
                    continue
                if split.end < len(raw_text):
                    new_offset = offset + _byte_end(chunk_to_consume, raw_text, split.end)
                    raw_text = raw_text[: split.end]
            if offset == 0 and saved_header is None and split.records:
                saved_header = split.records[0].fields

        chunks.append(
            RawChunk(
                rel_posix=rel_posix,
                content=raw_text,
                start_offset=offset,
                new_offset=new_offset,
                size=cur_size,
                mtime=cur_mtime,
                csv_header=saved_header,
            )
        )
        files_cursor[rel_posix] = {
            "size": cur_size,
            "mtime": cur_mtime,
            "offset": new_offset,
            "csv_header": saved_header,
        }

    return RawReadOutcome(
        chunks=chunks,
        cursor={"files": files_cursor},
        last_error=last_error,
    )
