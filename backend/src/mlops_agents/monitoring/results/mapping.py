"""In-process mapping evaluator for monitoring results (I22)."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

from mlops_agents.monitoring.expr import evaluate
from mlops_agents.monitoring.results.contract import (
    ParseOutcome,
    ParserMapping,
    ResultRecord,
)

logger = logging.getLogger(__name__)


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
    index: int,
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
    source_key = str(custom_key) if custom_key is not None else f"{origin_ref}:{index}"

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
) -> ParseOutcome:
    """Evaluates a ParserMapping against raw text content."""
    records: list[tuple[str, ResultRecord]] = []
    errors: list[str] = []

    fmt = mapping.format

    if fmt == "jsonl":
        for idx, line in enumerate(raw.splitlines()):
            line_str = line.strip()
            if not line_str:
                continue
            try:
                raw_parsed: object = json.loads(line_str)
                if not isinstance(raw_parsed, dict):
                    errors.append(f"Line {idx} is not a JSON object")
                    continue
                data = cast(dict[str, Any], raw_parsed)
                k, rec = _map_row(data, mapping, origin_ref, idx)
                records.append((k, rec))
            except Exception as exc:
                errors.append(f"Line {idx}: {exc}")

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
        try:
            reader = csv.DictReader(io.StringIO(raw))
            for idx, row in enumerate(reader):
                try:
                    row_dict = cast(dict[str, Any], dict(row))
                    k, rec = _map_row(row_dict, mapping, origin_ref, idx)
                    records.append((k, rec))
                except Exception as exc:
                    errors.append(f"Row {idx}: {exc}")
        except Exception as exc:
            errors.append(f"Invalid CSV: {exc}")

    elif fmt == "log":
        pattern: re.Pattern[str] | None = None
        if mapping.pattern:
            pattern = re.compile(mapping.pattern)

        for idx, line in enumerate(raw.splitlines()):
            line_str = line.strip()
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
                k, rec = _map_row(row_data, mapping, origin_ref, idx)
                records.append((k, rec))
            except Exception as exc:
                errors.append(f"Line {idx}: {exc}")

    return ParseOutcome(records=records, errors=errors)
