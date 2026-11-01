"""In-process mapping evaluator for monitoring results (I22)."""

from __future__ import annotations

import csv
import json
import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from mlops_agents.monitoring.expr import evaluate
from mlops_agents.monitoring.results.constants import HELD_TAIL_QUIET_SECONDS
from mlops_agents.monitoring.results.contract import (
    ParseOutcome,
    ParserMapping,
    ResultRecord,
)

logger = logging.getLogger(__name__)

# The mapping formats read line by line, whose last line may still be being written
LineFormat = Literal["jsonl", "csv", "log"]


@dataclass(frozen=True)
class CsvRecord:
    """One complete CSV record: its fields, or the reader's error, at its first line's offset."""

    offset: int
    fields: list[str] | None
    error: str | None = None


@dataclass(frozen=True)
class CsvSplit:
    records: list[CsvRecord]
    # Text index just past the last complete record; an unfinished final record starts here.
    end: int
    # Byte offset of a final record the text ends inside of (an open quoted field), if any.
    unfinished_offset: int | None = None


def split_csv_records(text: str, start_offset: int) -> CsvSplit:
    """Splits CSV text into complete records, a quoted field may span lines.

    Lines are cut on "\\n" only, the reader's newline rule. A final record still inside a
    quoted field when the text ends is left out, so the reader can hold it until it is
    complete; its offset is kept in `unfinished_offset`."""
    pieces = text.split("\n")
    lines = [p + "\n" for p in pieces[:-1]]
    if pieces[-1]:
        lines.append(pieces[-1])

    # The reader pulls a line only when it needs one, so after each record these hold the
    # byte offset and text index of the next unread line.
    next_offset = start_offset
    next_index = 0
    exhausted = False

    def _feed() -> Iterator[str]:
        nonlocal next_offset, next_index, exhausted
        for line in lines:
            next_offset += len(line.encode("utf-8"))
            next_index += len(line)
            yield line
        exhausted = True

    reader = csv.reader(_feed())
    records: list[CsvRecord] = []
    end = 0
    unfinished_offset: int | None = None
    while True:
        record_offset = next_offset
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            records.append(CsvRecord(offset=record_offset, fields=None, error=str(exc)))
            end = next_index
            continue
        if exhausted:
            # The text ended inside a quoted field: the reader returned a partial record
            unfinished_offset = record_offset
            break
        end = next_index
        if row and not (len(row) == 1 and not row[0].strip()):
            records.append(CsvRecord(offset=record_offset, fields=row))
    return CsvSplit(records=records, end=end, unfinished_offset=unfinished_offset)


def is_complete_record(
    format: LineFormat,
    text: str,
    *,
    pattern: str | None = None,
    csv_header: list[str] | None = None,
) -> bool:
    """Whether `text`, a file's last record without its newline, is a whole record of `format`.

    csv: one record with no open quote and the header's number of fields (with no header known,
    the record is the header itself); jsonl: a JSON object; log: `pattern` matches, or the
    line is not empty when there is no pattern. A blank tail holds no record to break."""
    if not text.strip():
        return True
    if format == "jsonl":
        try:
            return isinstance(json.loads(text), dict)
        except ValueError:
            return False
    if format == "log":
        line = text.strip()
        return re.search(pattern, line) is not None if pattern else bool(line)
    split = split_csv_records(text, 0)
    if split.unfinished_offset is not None or split.end != len(text) or len(split.records) != 1:
        return False
    fields = split.records[0].fields
    return fields is not None and (csv_header is None or len(fields) == len(csv_header))


def incomplete_record_error(offset: int) -> str:
    """The error for a last record at `offset` that is still incomplete once its file has
    been unchanged for the quiet time."""
    return (
        f"Offset {offset}: last record is incomplete and the file has not changed for"
        f" {HELD_TAIL_QUIET_SECONDS} s"
    )


def _get_path_val(data: Mapping[str, Any], path: str) -> object | None:
    """Extract a value from nested dicts/lists using dot notation."""
    parts = path.split(".")
    cur: object = data
    for part in parts:
        if isinstance(cur, Mapping) and part in cur:
            mapping_cur = cast(Mapping[str, object], cur)
            cur = mapping_cur[part]
        elif isinstance(cur, (list, tuple)):
            seq_cur = cast(list[object] | tuple[object, ...], cur)
            try:
                idx = int(part)
                cur = seq_cur[idx]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _parse_timestamp(val: Any) -> datetime:
    if isinstance(val, datetime):
        return val if val.tzinfo is not None else val.replace(tzinfo=UTC)
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val, tz=UTC)
    if isinstance(val, str):
        # Support trailing 'Z'
        cleaned = val.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    raise ValueError(f"Cannot parse timestamp from {val!r}")


def _parse_float(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _map_row(
    raw_item: dict[str, Any],
    mapping: ParserMapping,
    origin_ref: str,
    key_suffix: str | int,
) -> tuple[str, ResultRecord]:
    # Start with defaults
    item_dict: dict[str, Any] = dict(mapping.defaults)

    # Map fields
    for target_field, src_path in mapping.fields.items():
        extracted = _get_path_val(raw_item, src_path)
        if extracted is not None:
            item_dict[target_field] = extracted

    # Determine source_key
    custom_key = item_dict.pop("source_key", None) or item_dict.pop("key", None)
    source_key = str(custom_key) if custom_key is not None else f"{origin_ref}:{key_suffix}"

    # Force origin to 'team'
    item_dict["origin"] = "team"

    # Set default source_ref
    if "source_ref" not in item_dict or not item_dict["source_ref"]:
        item_dict["source_ref"] = origin_ref

    # Convert types
    if "ran_at" in item_dict:
        item_dict["ran_at"] = _parse_timestamp(item_dict["ran_at"])
    if "value" in item_dict and not isinstance(item_dict["value"], (int, float)):
        item_dict["value"] = _parse_float(item_dict["value"])
    if "threshold" in item_dict and not isinstance(item_dict["threshold"], (int, float)):
        item_dict["threshold"] = _parse_float(item_dict["threshold"])

    # Detail defaults to {}
    if "detail" not in item_dict or not isinstance(item_dict["detail"], dict):
        item_dict["detail"] = {}

    # Evaluate fired_when condition
    fired = evaluate(mapping.fired_when, item_dict)
    item_dict["fired"] = fired

    rec = ResultRecord.model_validate(item_dict)
    return source_key, rec


def evaluate_mapping(
    mapping: ParserMapping,
    raw: str,
    origin_ref: str,
    *,
    start_offset: int = 0,
    csv_header: list[str] | None = None,
) -> ParseOutcome:
    """Evaluates a ParserMapping against raw text content."""
    records: list[tuple[str, ResultRecord]] = []
    errors: list[str] = []

    fmt = mapping.format

    if fmt == "jsonl":
        cur_offset = start_offset
        for raw_line in raw.splitlines(keepends=True):
            line_offset = cur_offset
            cur_offset += len(raw_line.encode("utf-8"))
            line_str = raw_line.strip()
            if not line_str:
                continue
            try:
                raw_parsed: object = json.loads(line_str)
                if not isinstance(raw_parsed, dict):
                    errors.append(f"Offset {line_offset} is not a JSON object")
                    continue
                data = cast(dict[str, Any], raw_parsed)
                k, rec = _map_row(data, mapping, origin_ref, line_offset)
                records.append((k, rec))
            except Exception as exc:
                errors.append(f"Offset {line_offset}: {exc}")

    elif fmt == "json":
        try:
            raw_parsed: object = json.loads(raw)
            if isinstance(raw_parsed, list):
                list_items = cast(list[object], raw_parsed)
                for idx, item_obj in enumerate(list_items):
                    if not isinstance(item_obj, dict):
                        errors.append(f"Item {idx} is not a JSON object")
                        continue
                    try:
                        item = cast(dict[str, Any], item_obj)
                        k, rec = _map_row(item, mapping, origin_ref, idx)
                        records.append((k, rec))
                    except Exception as exc:
                        errors.append(f"Item {idx}: {exc}")
            elif isinstance(raw_parsed, dict):
                try:
                    data = cast(dict[str, Any], raw_parsed)
                    k, rec = _map_row(data, mapping, origin_ref, 0)
                    records.append((k, rec))
                except Exception as exc:
                    errors.append(f"Root: {exc}")
            else:
                errors.append("JSON root must be an object or array of objects")
        except Exception as exc:
            errors.append(f"Invalid JSON: {exc}")

    elif fmt == "csv":
        # The reader passes complete records only: it holds an unfinished one, or reports it
        csv_records = split_csv_records(raw, start_offset).records
        if start_offset == 0 and csv_records:
            header_record, *csv_records = csv_records
            csv_header = header_record.fields
            if header_record.error is not None:
                errors.append(f"Offset {header_record.offset}: {header_record.error}")

        for csv_record in csv_records:
            if csv_record.fields is None:
                errors.append(f"Offset {csv_record.offset}: {csv_record.error}")
                continue
            if csv_header:
                row_dict = dict(zip(csv_header, csv_record.fields, strict=False))
            else:
                row_dict = {f"col_{i}": v for i, v in enumerate(csv_record.fields)}
            try:
                k, rec = _map_row(row_dict, mapping, origin_ref, csv_record.offset)
                records.append((k, rec))
            except Exception as exc:
                errors.append(f"Offset {csv_record.offset}: {exc}")

    elif fmt == "log":
        pattern: re.Pattern[str] | None = None
        if mapping.pattern:
            pattern = re.compile(mapping.pattern)

        cur_offset = start_offset
        for raw_line in raw.splitlines(keepends=True):
            line_offset = cur_offset
            cur_offset += len(raw_line.encode("utf-8"))
            line_str = raw_line.strip()
            if not line_str:
                continue
            if pattern is not None:
                m = pattern.search(line_str)
                if not m:
                    continue
                row_data: dict[str, Any] = m.groupdict()
            else:
                row_data = {"message": line_str}

            try:
                k, rec = _map_row(row_data, mapping, origin_ref, line_offset)
                records.append((k, rec))
            except Exception as exc:
                errors.append(f"Offset {line_offset}: {exc}")

    return ParseOutcome(records=records, errors=errors)
