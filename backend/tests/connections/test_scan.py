"""Tests for table scanning, data versions, DuckDB source generation and findings walk."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from mlops_agents.connections.contract import LocalFolderSettings, TableRef
from mlops_agents.connections.kinds.local_folder import LocalFolderKind
from mlops_agents.errors import DomainError


def _make_link_or_junction(target: Path, link: Path) -> bool:
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except OSError:
        if os.name == "nt":
            res = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
            )
            return res.returncode == 0
        return False


def test_tables_scan_formats_and_rules(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    data.mkdir()

    # 1. Top-level files: csv, parquet, json, jsonl
    (data / "users.csv").write_text("id,name\n1,alice", encoding="utf-8")
    (data / "items.parquet").write_bytes(b"PAR1dummy")
    (data / "config.json").write_text('{"key": "value"}', encoding="utf-8")
    (data / "logs.jsonl").write_text('{"msg": "hi"}\n', encoding="utf-8")

    # 2. Upper-case extensions
    (data / "CAPS.CSV").write_text("x,y\n1,2", encoding="utf-8")

    # 3. Subfolder of Parquet parts as one table
    events_dir = data / "events"
    events_dir.mkdir()
    (events_dir / "part-1.parquet").write_bytes(b"PAR1")
    (events_dir / "part-2.parquet").write_bytes(b"PAR1")
    # Dotfile in subfolder is ignored
    (events_dir / ".hidden").write_text("ignore me", encoding="utf-8")

    # 4. Mixed subfolder unsupported
    mixed_dir = data / "mixed"
    mixed_dir.mkdir()
    (mixed_dir / "a.parquet").write_bytes(b"PAR1")
    (mixed_dir / "b.csv").write_text("1,2", encoding="utf-8")

    # 5. Unsupported file format (.xlsx)
    (data / "sheet.xlsx").write_bytes(b"PK")

    kind = LocalFolderKind(home)
    settings = LocalFolderSettings(path=str(data))
    tables = kind.tables(settings)

    table_names = [t.name for t in tables]
    assert "users" in table_names
    assert "items" in table_names
    assert "config" in table_names
    assert "logs" in table_names
    assert "CAPS" in table_names
    assert "events" in table_names

    # Check events subfolder table
    events_table = next(t for t in tables if t.name == "events")
    assert events_table.format == "parquet"
    assert events_table.files == ["events/part-1.parquet", "events/part-2.parquet"]

    # Mixed subfolder and .xlsx are not tables
    assert "mixed" not in table_names
    assert "sheet" not in table_names


def test_table_name_collision(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    data.mkdir()

    # Name collision: a.csv and a.parquet -> become a.csv and a.parquet
    (data / "a.csv").write_text("x\n1", encoding="utf-8")
    (data / "a.parquet").write_bytes(b"PAR1")
    (data / "unique.json").write_text("{}", encoding="utf-8")

    kind = LocalFolderKind(home)
    tables = kind.tables(LocalFolderSettings(path=str(data)))

    names = [t.name for t in tables]
    assert "a.csv" in names
    assert "a.parquet" in names
    assert "unique" in names


def test_table_links_and_too_many_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    data.mkdir()

    # Link in root folder is never a table
    real_target = tmp_path / "target"
    real_target.mkdir()
    (real_target / "linked.csv").write_text("a,b", encoding="utf-8")

    link_entry = data / "link_folder"
    has_links = _make_link_or_junction(real_target, link_entry)

    # Subfolder with link inside makes whole subfolder unsupported
    sub_with_link = data / "sub_with_link"
    sub_with_link.mkdir()
    (sub_with_link / "real.parquet").write_bytes(b"PAR1")
    sub_inner_link = sub_with_link / "inner_link"
    if has_links:
        _make_link_or_junction(real_target, sub_inner_link)

    kind = LocalFolderKind(home)
    tables = kind.tables(LocalFolderSettings(path=str(data)))
    names = [t.name for t in tables]

    if has_links:
        assert "link_folder" not in names
        assert "sub_with_link" not in names

    # Findings verify reasons
    findings = kind.findings(LocalFolderSettings(path=str(data)))
    unsupported_reasons = {f.path: f.reason for f in findings.unsupported_files}
    if has_links:
        assert unsupported_reasons.get("link_folder") == "a link"
        assert unsupported_reasons.get("sub_with_link") == "a link"


def test_table_too_many_files_unsupported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    data.mkdir()

    large_sub = data / "large_table"
    large_sub.mkdir()
    for i in range(5):
        (large_sub / f"part_{i}.parquet").write_bytes(b"PAR1")

    # Lower cap for test
    import mlops_agents.connections.kinds.local_folder_scan as scan_module

    monkeypatch.setattr(scan_module, "MAX_TABLE_FILES", 3)

    kind = LocalFolderKind(home)
    tables = kind.tables(LocalFolderSettings(path=str(data)))
    assert "large_table" not in [t.name for t in tables]

    findings = kind.findings(LocalFolderSettings(path=str(data)))
    unsupported = {f.path: f.reason for f in findings.unsupported_files}
    assert unsupported.get("large_table") == "too many files"


def test_data_version(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    data.mkdir()

    csv_file = data / "sales.csv"
    csv_file.write_text("id,amount\n1,100", encoding="utf-8")

    kind = LocalFolderKind(home)
    settings = LocalFolderSettings(path=str(data))
    table = TableRef(name="sales", files=["sales.csv"], format="csv")

    v1 = kind.data_version(settings, table)
    assert len(v1) == 16
    assert kind.data_version(settings, table) == v1  # Stable

    # Content/size changes
    csv_file.write_text("id,amount\n1,100\n2,200", encoding="utf-8")
    v2 = kind.data_version(settings, table)
    assert v2 != v1

    # mtime changes without size change
    time.sleep(0.01)
    stat = csv_file.stat()
    os.utime(csv_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    v3 = kind.data_version(settings, table)
    assert v3 != v2

    # Vanished file raises DomainError("connection_invalid")
    csv_file.unlink()
    with pytest.raises(DomainError) as err:
        kind.data_version(settings, table)
    assert err.value.code == "connection_invalid"


def test_duckdb_source(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    data.mkdir()

    kind = LocalFolderKind(home)
    settings = LocalFolderSettings(path=str(data))

    # Single CSV
    t_csv = TableRef(name="users", files=["users.csv"], format="csv")
    src_csv = kind.duckdb_source(settings, t_csv)
    expected_path = (data.resolve() / "users.csv").as_posix()
    assert src_csv.sql_from == f"read_csv(['{expected_path}'])"
    assert src_csv.allowed_directories == [data.resolve()]

    # Multi-file Parquet
    t_par = TableRef(
        name="events",
        files=["events/part1.parquet", "events/part2.parquet"],
        format="parquet",
    )
    src_par = kind.duckdb_source(settings, t_par)
    p1 = (data.resolve() / "events/part1.parquet").as_posix()
    p2 = (data.resolve() / "events/part2.parquet").as_posix()
    assert src_par.sql_from == f"read_parquet(['{p1}', '{p2}'], union_by_name=true)"

    # Single JSONL
    t_jsonl = TableRef(name="logs", files=["logs.jsonl"], format="jsonl")
    src_jsonl = kind.duckdb_source(settings, t_jsonl)
    p_log = (data.resolve() / "logs.jsonl").as_posix()
    assert src_jsonl.sql_from == f"read_json(['{p_log}'], format='newline_delimited')"

    # File with single quote in filename is escaped
    t_quote = TableRef(name="q", files=["user's_data.csv"], format="csv")
    src_quote = kind.duckdb_source(settings, t_quote)
    p_q = (data.resolve() / "user's_data.csv").as_posix().replace("'", "''")
    assert f"'{p_q}'" in src_quote.sql_from


def test_findings_walk_secrets_and_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    data.mkdir()

    # Secret files
    (data / ".env").write_text("SECRET=1", encoding="utf-8")
    (data / "prod.pem").write_text("PEM_DATA", encoding="utf-8")
    sub = data / "sub"
    sub.mkdir()
    (sub / "id_rsa").write_text("RSA_KEY", encoding="utf-8")
    (sub / "normal.txt").write_text("not secret", encoding="utf-8")

    # Regular table file
    (data / "data.csv").write_text("a,b", encoding="utf-8")
    # Unsupported file
    (data / "doc.pdf").write_bytes(b"PDF")

    kind = LocalFolderKind(home)
    findings = kind.findings(LocalFolderSettings(path=str(data)))

    secret_paths = [s.path for s in findings.secret_files]
    assert secret_paths == [".env", "prod.pem", "sub/id_rsa"]
    for s in findings.secret_files:
        assert s.reason == "matches a secret file name"

    unsupported_paths = [u.path for u in findings.unsupported_files]
    assert "doc.pdf" in unsupported_paths
    assert findings.truncated is False

    # Truncation when scan limit exceeded
    import mlops_agents.connections.kinds.local_folder_scan as scan_module

    monkeypatch.setattr(scan_module, "MAX_SCAN_ENTRIES", 3)
    trunc_findings = kind.findings(LocalFolderSettings(path=str(data)))
    assert trunc_findings.truncated is True


def test_findings_walk_secret_cap_100(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    data.mkdir()

    # Create 105 secret files (e.g. .pem files)
    for i in range(105):
        (data / f"secret_{i:03d}.pem").write_text("KEY", encoding="utf-8")

    kind = LocalFolderKind(home)
    findings = kind.findings(LocalFolderSettings(path=str(data)))
    assert findings.truncated is True
    assert len(findings.secret_files) == 100
