"""File source reader for monitoring results (I22)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from mlops_agents.connections.contract import LOCAL_FOLDER_KIND, Connections
from mlops_agents.credentials.scrub import scrub_url_secrets
from mlops_agents.monitoring.results.constants import (
    CODE_PARSER_CHUNK_BYTES,
    HELD_TAIL_QUIET_SECONDS,
)
from mlops_agents.monitoring.results.contract import ParserMapping, ResultRecord, parse_source_id
from mlops_agents.monitoring.results.mapping import (
    LineFormat,
    incomplete_record_error,
    is_complete_record,
    split_csv_records,
)
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
    # Byte offset of a last record left out of `content` as incomplete after the quiet time;
    # `new_offset` is past it
    incomplete_tail_offset: int | None = None


@dataclass
class RawReadOutcome:
    chunks: list[RawChunk]
    cursor: dict[str, Any]
    last_error: str | None = None


def _quiet(mtime: float, now: datetime) -> bool:
    """Whether the file has been unchanged for the quiet time, so its writer has stopped."""
    return now.timestamp() - mtime >= HELD_TAIL_QUIET_SECONDS


@dataclass(frozen=True)
class _TailCut:
    deliver: int  # bytes of the chunk handed on
    consume: int  # bytes the cursor moves past; any past `deliver` are an incomplete record


def _cut_tail(
    chunk: bytes, records_end: int, quiet: bool, complete: Callable[[str], bool] | None
) -> _TailCut:
    """How much of a chunk read from the cursor to hand on; its complete records end at
    `records_end`.

    The bytes after them (the tail) may be a record the writer is still writing, so they are
    held until the file has been quiet for HELD_TAIL_QUIET_SECONDS. Then a complete tail is
    handed on, and an incomplete one is consumed without being handed on. With `complete`
    None the consumer judges the tail itself, so a quiet tail is always handed on."""
    if records_end == len(chunk) or not quiet:
        return _TailCut(deliver=records_end, consume=records_end)
    if complete is None or complete(chunk[records_end:].decode("utf-8", errors="replace")):
        return _TailCut(deliver=len(chunk), consume=len(chunk))
    return _TailCut(deliver=records_end, consume=len(chunk))


def _complete_jsonl(tail: str) -> bool:
    """The standard results file's completeness rule: it is JSON Lines."""
    return is_complete_record("jsonl", tail)


def _read_files_sync(
    root: Path,
    pattern: str,
    secret_paths: set[str],
    cursor: dict[str, Any],
    consecutive_failures: int = 0,
    *,
    now: datetime,
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

        cut = _cut_tail(chunk, chunk.rfind(b"\n") + 1, _quiet(cur_mtime, now), _complete_jsonl)
        if cut.consume == 0:
            files_cursor[rel_posix] = {
                "size": cur_size,
                "mtime": cur_mtime,
                "offset": offset,
            }
            continue

        new_offset = offset + cut.consume
        lines = chunk[: cut.deliver].splitlines(keepends=True)
        # An incomplete last record is consumed as one line that takes the invalid-line path
        incomplete_at: int | None = None
        if cut.deliver < cut.consume:
            incomplete_at = offset + cut.deliver
            lines.append(chunk[cut.deliver : cut.consume])

        current_line_start = offset
        for raw_line_bytes in lines:
            line_offset = current_line_start
            current_line_start += len(raw_line_bytes)

            line_str = raw_line_bytes.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue

            try:
                if line_offset == incomplete_at:
                    raise ValueError(incomplete_record_error(line_offset))
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
        now: datetime,
        consecutive_failures: int = 0,
    ) -> SourceReadOutcome:
        """Reads new result records from files matching source_id pattern. A file's quiet
        time is judged against `now`, the collection time."""
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
                _read_files_sync,
                root,
                pattern,
                secret_paths,
                cursor,
                consecutive_failures,
                now=now,
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
        now: datetime,
        mapping: ParserMapping | None = None,
    ) -> RawReadOutcome:
        """Reads raw file contents from files matching source_id pattern, for `mapping`'s
        format (None: a code parser). A file's quiet time is judged against `now`, the
        collection time."""
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
                _read_raw_files_sync, root, pattern, secret_paths, cursor, mapping, now=now
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


def _line_pieces(data: bytes, bound: int) -> list[tuple[int, int]]:
    """`data` split in order into (start, end) byte ranges of at most `bound` bytes, each
    ending just past a newline: a line longer than `bound` is a range of its own, and the
    last range ends where `data` does."""
    pieces: list[tuple[int, int]] = []
    start = 0
    while start < len(data):
        end = len(data)
        if end - start > bound:
            cut = data.rfind(b"\n", start, start + bound)
            if cut != -1:
                end = cut + 1
            else:
                newline = data.find(b"\n", start)
                end = newline + 1 if newline != -1 else len(data)
        pieces.append((start, end))
        start = end
    return pieces


def _cut_chunk(
    chunk: bytes,
    offset: int,
    fmt: LineFormat | None,
    pattern: str | None,
    saved_header: list[str] | None,
    quiet: bool,
) -> tuple[_TailCut, list[str] | None]:
    """The cut of a chunk read from `offset` for a parser of format `fmt` (None: a code
    parser, whose script judges a tail itself), and the file's csv header."""
    newline_end = chunk.rfind(b"\n") + 1
    if fmt is None:
        return _cut_tail(chunk, newline_end, quiet, None), None
    if fmt != "csv":

        def complete(tail: str) -> bool:
            return is_complete_record(fmt, tail, pattern=pattern)

        return _cut_tail(chunk, newline_end, quiet, complete), None

    # A csv record may span lines inside quotes: its complete records end where the split of
    # the text up to the last newline says, so an unfinished quoted record is in the tail
    head = chunk[:newline_end]
    head_text = head.decode("utf-8", errors="replace")
    split = split_csv_records(head_text, offset)
    records_end = _byte_end(head, head_text, split.end)
    header = saved_header
    if offset == 0 and header is None and split.records:
        header = split.records[0].fields

    def complete_csv(tail: str) -> bool:
        return is_complete_record("csv", tail, csv_header=header)

    cut = _cut_tail(chunk, records_end, quiet, complete_csv)
    if offset == 0 and header is None and not split.records and cut.deliver == len(chunk):
        # The tail handed on is the file's first record: its header
        tail_records = split_csv_records(chunk.decode("utf-8", errors="replace"), 0).records
        header = tail_records[0].fields if tail_records else None
    return cut, header


def _read_raw_files_sync(
    root: Path,
    pattern: str,
    secret_paths: set[str],
    cursor: dict[str, Any],
    mapping: ParserMapping | None,
    *,
    now: datetime,
) -> RawReadOutcome:
    fmt = mapping.format if mapping is not None else None
    line_pattern = mapping.pattern if mapping is not None else None
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

        if fmt == "json":
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

        cut, csv_header = _cut_chunk(
            chunk, offset, fmt, line_pattern, saved_header, _quiet(cur_mtime, now)
        )
        new_offset = offset + cut.consume
        files_cursor[rel_posix] = {
            "size": cur_size,
            "mtime": cur_mtime,
            "offset": new_offset,
            "csv_header": csv_header,
        }
        if cut.consume == 0:
            continue

        if mapping is None:
            # A code parser's run keeps a bounded output, so it is given the file in pieces.
            # Surrogate escapes keep each piece's byte length exact for its run to read.
            chunks.extend(
                RawChunk(
                    rel_posix=rel_posix,
                    content=chunk[start:end].decode("utf-8", errors="surrogateescape"),
                    start_offset=offset + start,
                    new_offset=offset + end,
                    size=cur_size,
                    mtime=cur_mtime,
                )
                for start, end in _line_pieces(chunk[: cut.deliver], CODE_PARSER_CHUNK_BYTES)
            )
            continue

        chunks.append(
            RawChunk(
                rel_posix=rel_posix,
                content=chunk[: cut.deliver].decode("utf-8", errors="replace"),
                start_offset=offset,
                new_offset=new_offset,
                size=cur_size,
                mtime=cur_mtime,
                csv_header=csv_header,
                incomplete_tail_offset=offset + cut.deliver if cut.deliver < cut.consume else None,
            )
        )

    return RawReadOutcome(
        chunks=chunks,
        cursor={"files": files_cursor},
        last_error=last_error,
    )
