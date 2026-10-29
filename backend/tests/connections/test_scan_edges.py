"""Edge cases of the local folder scan: wildcard names, junctions in the walk, the caps, names."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import mlops_agents.connections.kinds.local_folder_scan as scan_module
from mlops_agents.connections.contract import LocalFolderSettings
from mlops_agents.connections.kinds.local_folder import LocalFolderKind
from mlops_agents.errors import DomainError
from tests.connections.helpers import make_junction, make_link_or_junction

WILDCARD = "a name with a wildcard"


def _kind(tmp_path: Path) -> LocalFolderKind:
    home = tmp_path / "home"
    home.mkdir()
    return LocalFolderKind(home)


def _data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    return data


def test_a_bracket_file_name_is_never_a_table(tmp_path: Path) -> None:
    kind = _kind(tmp_path)
    data = _data(tmp_path)
    (data / "a.csv").write_text("v\nA\n", encoding="utf-8")
    (data / "b.csv").write_text("v\nB\n", encoding="utf-8")
    (data / "[ab].csv").write_text("v\nLITERAL\n", encoding="utf-8")
    settings = LocalFolderSettings(path=str(data))

    tables = kind.tables(settings)
    assert [(t.name, t.files) for t in tables] == [("a", ["a.csv"]), ("b", ["b.csv"])]
    for table in tables:
        assert "[ab]" not in kind.duckdb_source(settings, table).sql_from

    unsupported = kind.findings(settings).unsupported_files
    assert [(u.path, u.reason) for u in unsupported] == [("[ab].csv", WILDCARD)]


@pytest.mark.skipif(os.name == "nt", reason="Windows file names cannot hold * or ?")
@pytest.mark.parametrize("name", ["*.csv", "a?.csv"])
def test_a_star_or_question_mark_file_name_is_never_a_table(tmp_path: Path, name: str) -> None:
    kind = _kind(tmp_path)
    data = _data(tmp_path)
    (data / "ab.csv").write_text("v\nA\n", encoding="utf-8")
    (data / name).write_text("v\nX\n", encoding="utf-8")
    settings = LocalFolderSettings(path=str(data))

    assert [t.name for t in kind.tables(settings)] == ["ab"]
    assert [(u.path, u.reason) for u in kind.findings(settings).unsupported_files] == [
        (name, WILDCARD)
    ]


def test_a_bracket_table_folder_or_part_makes_the_folder_unsupported(tmp_path: Path) -> None:
    kind = _kind(tmp_path)
    data = _data(tmp_path)
    bracket_dir = data / "fold[ab]"
    bracket_dir.mkdir()
    (bracket_dir / "x.parquet").write_bytes(b"PAR1")
    parts_dir = data / "parts"
    parts_dir.mkdir()
    (parts_dir / "p1.parquet").write_bytes(b"PAR1")
    (parts_dir / "p[12].parquet").write_bytes(b"PAR1")
    settings = LocalFolderSettings(path=str(data))

    assert kind.tables(settings) == []
    assert [(u.path, u.reason) for u in kind.findings(settings).unsupported_files] == [
        ("fold[ab]", WILDCARD),
        ("parts", WILDCARD),
    ]


def test_a_connection_folder_with_a_bracket_is_refused(tmp_path: Path) -> None:
    kind = _kind(tmp_path)
    folder = tmp_path / "data[2024]"
    folder.mkdir()

    with pytest.raises(DomainError) as err:
        kind.validate(LocalFolderSettings(path=str(folder)), None)
    assert err.value.code == "connection_invalid"
    assert err.value.message == "Path cannot contain *, ? or [ (DuckDB reads them as wildcards)."


def test_the_walk_does_not_enter_a_junction(tmp_path: Path) -> None:
    kind = _kind(tmp_path)
    data = _data(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".env").write_text("SECRET=1", encoding="utf-8")
    if not make_junction(outside, data / "jn"):
        pytest.skip("Windows junctions only")

    findings = kind.findings(LocalFolderSettings(path=str(data)))

    assert findings.secret_files == []
    assert findings.truncated is False


def test_the_walk_does_not_enter_a_link(tmp_path: Path) -> None:
    kind = _kind(tmp_path)
    data = _data(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".env").write_text("SECRET=1", encoding="utf-8")
    if not make_link_or_junction(outside, data / "ln"):
        pytest.skip("Symlinks and junctions not supported in this environment")

    assert kind.findings(LocalFolderSettings(path=str(data))).secret_files == []


def test_a_junction_does_not_count_its_target_against_the_entry_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kind = _kind(tmp_path)
    data = _data(tmp_path)
    big = tmp_path / "big"
    big.mkdir()
    for i in range(10):
        (big / f"f{i}.txt").write_text("x", encoding="utf-8")
    if not make_junction(big, data / "jn"):
        pytest.skip("Windows junctions only")
    monkeypatch.setattr(scan_module, "MAX_SCAN_ENTRIES", 5)

    assert kind.findings(LocalFolderSettings(path=str(data))).truncated is False


def test_exactly_100_secret_files_are_not_truncated(tmp_path: Path) -> None:
    kind = _kind(tmp_path)
    data = _data(tmp_path)
    for i in range(scan_module.MAX_SECRET_FINDINGS):
        (data / f"secret_{i:03d}.pem").write_text("KEY", encoding="utf-8")

    findings = kind.findings(LocalFolderSettings(path=str(data)))

    assert len(findings.secret_files) == scan_module.MAX_SECRET_FINDINGS
    assert findings.truncated is False


def test_the_entry_cap_counts_entries_not_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kind = _kind(tmp_path)
    data = _data(tmp_path)
    for i in range(6):
        (data / f"id_rsa_{i}").write_text("KEY", encoding="utf-8")
    monkeypatch.setattr(scan_module, "MAX_SCAN_ENTRIES", 3)

    findings = kind.findings(LocalFolderSettings(path=str(data)))

    assert findings.truncated is True
    assert len(findings.secret_files) <= 3


def test_entries_up_to_the_cap_are_not_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kind = _kind(tmp_path)
    data = _data(tmp_path)
    for i in range(3):
        (data / f"f{i}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(scan_module, "MAX_SCAN_ENTRIES", 3)

    assert kind.findings(LocalFolderSettings(path=str(data))).truncated is False


def test_a_stem_equal_to_another_full_name_gets_its_full_name(tmp_path: Path) -> None:
    kind = _kind(tmp_path)
    data = _data(tmp_path)
    (data / "a.csv").write_text("x\n1", encoding="utf-8")
    (data / "a.parquet").write_bytes(b"PAR1")
    (data / "a.csv.json").write_text("{}", encoding="utf-8")

    names = [t.name for t in kind.tables(LocalFolderSettings(path=str(data)))]

    assert names == ["a.csv", "a.csv.json", "a.parquet"]


def test_a_stem_equal_to_a_folder_name_gets_its_full_name(tmp_path: Path) -> None:
    kind = _kind(tmp_path)
    data = _data(tmp_path)
    (data / "a").mkdir()
    (data / "a" / "p.parquet").write_bytes(b"PAR1")
    (data / "a.csv").write_text("x\n1", encoding="utf-8")

    names = [t.name for t in kind.tables(LocalFolderSettings(path=str(data)))]

    assert names == ["a", "a.csv"]


def test_a_listing_failure_names_no_resolved_path(tmp_path: Path) -> None:
    # The kind names the folder as the user typed it; the scan only sees the resolved one.
    gone = tmp_path / "gone"

    with pytest.raises(DomainError) as err:
        scan_module.scan_tables_and_unsupported(gone)
    assert err.value.code == "connection_invalid"
    assert err.value.message.startswith("The folder could not be listed (")
    assert str(gone) not in err.value.message
