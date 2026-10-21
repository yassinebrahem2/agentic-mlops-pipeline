"""Writes the API contract to frontend/openapi.json, or checks it is current (--check)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from mlops_agents.api.app import create_app
from mlops_agents.config import RunMode, load_settings

# backend/src/mlops_agents/api/openapi_export.py -> the repository root.
OPENAPI_FILE = Path(__file__).resolve().parents[4] / "frontend" / "openapi.json"
OUT_OF_DATE = (
    "frontend/openapi.json is out of date. Run: uv run python -m mlops_agents.api.openapi_export"
)


def export_text() -> str:
    app = create_app(load_settings(RunMode.LOCAL, env={}))
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main(argv: Sequence[str] | None = None, *, target: Path = OPENAPI_FILE) -> int:
    parser = argparse.ArgumentParser(prog="python -m mlops_agents.api.openapi_export")
    parser.add_argument("--check", action="store_true", help="fail if the file is out of date")
    args = parser.parse_args(argv)
    text = export_text()
    if args.check:
        # Text mode reads CRLF as LF: a Windows checkout may convert line endings.
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current != text:
            print(OUT_OF_DATE, file=sys.stderr)
            return 1
        return 0
    target.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
