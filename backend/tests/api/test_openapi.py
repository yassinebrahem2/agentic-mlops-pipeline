import json
from pathlib import Path

import pytest

from mlops_agents.api.openapi_export import OPENAPI_FILE, export_text, main


def test_component_names_are_unique_model_names() -> None:
    schemas = json.loads(export_text()).get("components", {}).get("schemas", {})

    assert [name for name in schemas if "__" in name] == []


def test_committed_contract_matches_a_fresh_export() -> None:
    assert OPENAPI_FILE.read_text(encoding="utf-8") == export_text()
    assert main(["--check"]) == 0


def test_export_is_byte_identical_when_run_twice(tmp_path: Path) -> None:
    target = tmp_path / "openapi.json"

    assert main([], target=target) == 0
    first = target.read_bytes()
    assert main([], target=target) == 0

    assert target.read_bytes() == first
    assert first.endswith(b"}\n")
    assert b"\r\n" not in first


def test_check_accepts_crlf_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "openapi.json"
    target.write_bytes(export_text().replace("\n", "\r\n").encode())

    assert main(["--check"], target=target) == 0


def test_check_fails_with_the_fix_when_the_file_differs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = tmp_path / "openapi.json"
    stale.write_text("{}\n", encoding="utf-8")

    assert main(["--check"], target=stale) == 1
    assert (
        "frontend/openapi.json is out of date. "
        "Run: uv run python -m mlops_agents.api.openapi_export"
    ) in capsys.readouterr().err
