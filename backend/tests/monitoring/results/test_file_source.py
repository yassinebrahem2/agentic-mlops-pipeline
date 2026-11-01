"""Tests for FileSourceReader and file reading mechanics (I22)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from mlops_agents.connections.contract import LOCAL_FOLDER_KIND
from mlops_agents.connections.kinds.local_folder import LocalFolderKind
from mlops_agents.connections.registry import KindRegistry
from mlops_agents.connections.service import ConnectionsService
from mlops_agents.events.log import SqliteEventLog
from mlops_agents.monitoring.expr import Compare
from mlops_agents.monitoring.results.constants import HELD_TAIL_QUIET_SECONDS
from mlops_agents.monitoring.results.contract import ParserMapping, ResultRecord, source_id
from mlops_agents.monitoring.results.file_source import FileSourceReader
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.monitoring.results.conftest import FakeConnections, set_mtime

# The collection time the reader judges a file's quiet time against
_NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
_CSV_MAPPING = ParserMapping(
    format="csv",
    fields={"check_id": "id", "ran_at": "time", "subject": "note"},
    fired_when=Compare(op="eq", field="fired", value=True),
    defaults={"category": "data_quality"},
)


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "fixtures" / "results"


async def test_fixture_file_becomes_standard_records(
    tmp_path: Path, fixtures_dir: Path, project_id: str
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    fixture_content = (fixtures_dir / "standard_results.jsonl").read_bytes()
    target_file = data_dir / "results.jsonl"
    target_file.write_bytes(fixture_content)

    connections = FakeConnections()
    connections.add_local_folder("data_conn", data_dir)

    reader = FileSourceReader(connections)
    s_id = source_id("file", "results.jsonl", "data_conn")

    outcome = await reader.read(project_id, s_id, cursor={}, now=_NOW)
    assert len(outcome.records) == 3
    assert outcome.consecutive_failures == 0
    assert outcome.last_error is None

    r1, r2, r3 = outcome.records
    assert r1[0] == "results.jsonl:0"
    assert isinstance(r1[1], ResultRecord)
    assert r1[1].check_id == "chk_null_rate"
    assert r1[1].origin == "agent"
    assert r1[1].fired is False

    assert r2[1].check_id == "chk_drift"
    assert r2[1].fired is True

    assert r3[1].check_id == "chk_accuracy"
    assert r3[1].detail == {"f1": 0.91}

    # Cursor should have recorded the file offset at end of file
    assert "files" in outcome.cursor
    assert "results.jsonl" in outcome.cursor["files"]
    assert outcome.cursor["files"]["results.jsonl"]["offset"] == len(fixture_content)


async def test_half_written_last_line_waits_for_its_newline(
    tmp_path: Path, fixtures_dir: Path, project_id: str
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    half_content = (fixtures_dir / "half_line.jsonl").read_bytes()
    target_file = data_dir / "results.jsonl"
    target_file.write_bytes(half_content)
    set_mtime(target_file, _NOW)

    connections = FakeConnections()
    connections.add_local_folder("data_conn", data_dir)
    reader = FileSourceReader(connections)
    s_id = source_id("file", "results.jsonl", "data_conn")

    outcome1 = await reader.read(project_id, s_id, cursor={}, now=_NOW)
    # Only 1 complete line ended in \n
    assert len(outcome1.records) == 1
    assert outcome1.records[0][1].check_id == "chk_valid_1"

    # Now append rest of second line with newline
    with target_file.open("ab") as f:
        f.write(
            b', "fired": false, "category": "data_quality", "subject": "features", "detail": {}, "source_ref": "ref"}\n'
        )

    outcome2 = await reader.read(project_id, s_id, cursor=outcome1.cursor, now=_NOW)
    assert len(outcome2.records) == 1
    assert outcome2.records[0][1].check_id == "chk_half"


async def test_appended_file_is_read_from_its_offset(tmp_path: Path, project_id: str) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "results.jsonl"
    line1 = b'{"check_id": "chk_1", "origin": "agent", "ran_at": "2026-10-30T10:00:00Z", "fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    target.write_bytes(line1)

    connections = FakeConnections()
    connections.add_local_folder("data_conn", data_dir)
    reader = FileSourceReader(connections)
    s_id = source_id("file", "*.jsonl", "data_conn")

    out1 = await reader.read(project_id, s_id, cursor={}, now=_NOW)
    assert len(out1.records) == 1
    assert out1.records[0][1].check_id == "chk_1"

    # Second read without change
    out_noop = await reader.read(project_id, s_id, cursor=out1.cursor, now=_NOW)
    assert len(out_noop.records) == 0

    # Append second line
    line2 = b'{"check_id": "chk_2", "origin": "agent", "ran_at": "2026-10-30T10:05:00Z", "fired": true, "category": "drift", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    with target.open("ab") as f:
        f.write(line2)

    out2 = await reader.read(project_id, s_id, cursor=out1.cursor, now=_NOW)
    assert len(out2.records) == 1
    assert out2.records[0][1].check_id == "chk_2"
    assert out2.records[0][0] == f"results.jsonl:{len(line1)}"


async def test_truncated_or_rewritten_file_is_read_from_start(
    tmp_path: Path, project_id: str
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "results.jsonl"
    line1 = b'{"key": "k1", "check_id": "chk_1", "origin": "agent", "ran_at": "2026-10-30T10:00:00Z", "fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    line2 = b'{"key": "k2", "check_id": "chk_2", "origin": "agent", "ran_at": "2026-10-30T10:05:00Z", "fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    target.write_bytes(line1 + line2)

    connections = FakeConnections()
    connections.add_local_folder("data_conn", data_dir)
    reader = FileSourceReader(connections)
    s_id = source_id("file", "*.jsonl", "data_conn")

    out1 = await reader.read(project_id, s_id, cursor={}, now=_NOW)
    assert len(out1.records) == 2

    # Now rewrite file with only line3 (smaller size than line1+line2)
    line3 = b'{"key": "k3", "check_id": "chk_3", "origin": "agent", "ran_at": "2026-10-30T10:10:00Z", "fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    target.write_bytes(line3)

    out2 = await reader.read(project_id, s_id, cursor=out1.cursor, now=_NOW)
    assert len(out2.records) == 1
    assert out2.records[0][0] == "k3"
    assert out2.records[0][1].check_id == "chk_3"


async def test_invalid_line_recorded_on_source_and_does_not_stop_next_lines(
    tmp_path: Path, fixtures_dir: Path, project_id: str
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    content = (fixtures_dir / "with_invalid.jsonl").read_bytes()
    target = data_dir / "results.jsonl"
    target.write_bytes(content)

    connections = FakeConnections()
    connections.add_local_folder("data_conn", data_dir)
    reader = FileSourceReader(connections)
    s_id = source_id("file", "results.jsonl", "data_conn")

    out = await reader.read(project_id, s_id, cursor={}, now=_NOW)
    # Valid lines 1 and 3 are yielded
    assert len(out.records) == 2
    assert out.records[0][1].check_id == "chk_valid_1"
    assert out.records[1][1].check_id == "chk_valid_2"

    # Source records error and failure count
    assert out.consecutive_failures == 1
    assert out.last_error is not None
    # Cursor moved past all lines
    assert out.cursor["files"]["results.jsonl"]["offset"] == len(content)


async def test_pattern_with_dotdot_symlink_out_of_root_and_env_store_nothing(
    tmp_path: Path, project_id: str
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir(parents=True, exist_ok=True)

    outside_file = outside_dir / "secret.jsonl"
    outside_file.write_bytes(
        b'{"check_id": "chk_out", "origin": "agent", "ran_at": "2026-10-30T10:00:00Z", "fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    )

    # 1. Symlink out of root inside data_dir
    symlink_file = data_dir / "link_outside.jsonl"
    try:
        symlink_file.symlink_to(outside_file)
        has_symlink = True
    except OSError:
        has_symlink = False

    # 2. .env file
    env_file = data_dir / ".env"
    env_file.write_bytes(
        b'{"check_id": "chk_env", "origin": "agent", "ran_at": "2026-10-30T10:00:00Z", "fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    )

    connections = FakeConnections()
    # Mark .env in secret_files findings
    connections.add_local_folder("data_conn", data_dir, secret_files=[".env"])
    reader = FileSourceReader(connections)

    # A: Pattern with '..'
    s_id_dotdot = source_id("file", "../outside/*.jsonl", "data_conn")
    out_dotdot = await reader.read(project_id, s_id_dotdot, cursor={}, now=_NOW)
    assert len(out_dotdot.records) == 0

    # B: Symlink pointing out of root
    if has_symlink:
        s_id_symlink = source_id("file", "link_outside.jsonl", "data_conn")
        out_symlink = await reader.read(project_id, s_id_symlink, cursor={}, now=_NOW)
        assert len(out_symlink.records) == 0

    # C: .env file flagged by secret_files
    s_id_env = source_id("file", ".env", "data_conn")
    out_env = await reader.read(project_id, s_id_env, cursor={}, now=_NOW)
    assert len(out_env.records) == 0


async def test_secret_file_skip_against_real_connections_inspect(
    tmp_path: Path, project_id: str
) -> None:
    home = tmp_path / "home"
    paths = DataPaths(home)
    paths.ensure()
    databases = Databases(paths)
    await databases.app()
    await databases.project(project_id)
    events = SqliteEventLog(databases)
    registry = KindRegistry([LocalFolderKind(paths.root)])
    connections = ConnectionsService(databases, events, registry)

    # Prepare local folder connection directory
    data_dir = tmp_path / "data_folder"
    data_dir.mkdir(parents=True, exist_ok=True)
    sub_dir = data_dir / "sub"
    sub_dir.mkdir(parents=True, exist_ok=True)

    # 1. Top-level secret file: .env
    top_secret = data_dir / ".env"
    top_secret.write_bytes(
        b'{"check_id": "chk_top", "origin": "agent", "ran_at": "2026-10-30T10:00:00Z", "fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    )

    # 2. Nested secret file: sub/id_rsa
    nested_secret = sub_dir / "id_rsa"
    nested_secret.write_bytes(
        b'{"check_id": "chk_nested", "origin": "agent", "ran_at": "2026-10-30T10:00:00Z", "fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    )

    # 3. Legitimate results file: sub/results.jsonl
    legit_file = sub_dir / "results.jsonl"
    legit_file.write_bytes(
        b'{"check_id": "chk_valid", "origin": "agent", "ran_at": "2026-10-30T10:00:00Z", "fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    )

    # Verify real Connections.inspect finds both top-level and nested secret files
    findings = await connections.inspect(project_id, LOCAL_FOLDER_KIND, {"path": str(data_dir)})
    secret_paths = {f.path for f in findings.secret_files}
    assert ".env" in secret_paths
    assert "sub/id_rsa" in secret_paths

    # Create connection in real service
    await connections.create(
        project_id,
        LOCAL_FOLDER_KIND,
        "results-conn",
        {"path": str(data_dir)},
    )

    # Read files with recursive pattern "**/*"
    reader = FileSourceReader(connections)
    s_id = source_id("file", "**/*", "results-conn")
    outcome = await reader.read(project_id, s_id, cursor={}, now=_NOW)

    # Assert only the legitimate results file was read
    assert len(outcome.records) == 1
    assert outcome.records[0][1].check_id == "chk_valid"
    assert "sub/results.jsonl" in outcome.cursor.get("files", {})
    assert ".env" not in outcome.cursor.get("files", {})
    assert "sub/id_rsa" not in outcome.cursor.get("files", {})

    await databases.dispose()


async def test_file_source_reader_scrubs_credentials_in_error_and_log(
    tmp_path: Path, project_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    data_dir = tmp_path / "scrub_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "results.jsonl"

    secret_token = "secret_file_pass_12345"
    secret_param = "param_file_key_67890"
    raw_url = f"https://user:{secret_token}@fileserver.internal/path?api_key={secret_param}"

    # Site 3: An invalid line where parsing fails and the error text quotes the invalid origin value carrying the URL
    invalid_line = (
        f'{{"check_id": "c1", "origin": "invalid_origin_{raw_url}", '
        '"ran_at": "2026-10-30T10:00:00Z", "fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    ).encode()
    target.write_bytes(invalid_line)

    connections = FakeConnections()
    connections.add_local_folder("scrub_conn", data_dir)
    reader = FileSourceReader(connections)
    s_id = source_id("file", "*.jsonl", "scrub_conn")

    with caplog.at_level(logging.WARNING):
        out = await reader.read(project_id, s_id, cursor={}, now=_NOW)

    assert out.last_error is not None
    assert secret_token not in out.last_error
    assert secret_param not in out.last_error
    assert "***@" in out.last_error
    assert secret_token not in caplog.text
    assert secret_param not in caplog.text

    # Site 1: stat error carrying credential
    secret_stat = "stat_secret_token_999"
    stat_url = f"https://stat_user:{secret_stat}@stat.internal/path"
    orig_stat = Path.stat

    def failing_stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == "results.jsonl":
            raise OSError(f"Stat failed for {stat_url}")
        return orig_stat(self, *args, **kwargs)

    with patch.object(Path, "stat", failing_stat):
        out_stat = await reader.read(project_id, s_id, cursor={}, now=_NOW)
        assert out_stat.last_error is not None
        assert secret_stat not in out_stat.last_error
        assert "***@" in out_stat.last_error

    # Site 2: open error carrying credential
    secret_open = "open_secret_token_888"
    open_url = f"https://open_user:{secret_open}@open.internal/path"
    orig_open: Any = Path.open

    def failing_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == "results.jsonl":
            raise OSError(f"Open failed for {open_url}")
        res: Any = orig_open(self, *args, **kwargs)
        return res

    with patch.object(Path, "open", failing_open):
        out_open = await reader.read(project_id, s_id, cursor={}, now=_NOW)
        assert out_open.last_error is not None
        assert secret_open not in out_open.last_error
        assert "***@" in out_open.last_error


async def test_csv_raw_read_cuts_at_true_byte_offset_with_invalid_utf8(
    tmp_path: Path, project_id: str
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    # 0xE9 alone (a cp1252 e-acute) decodes to U+FFFD, three bytes when re-encoded
    content = b'id,note,time\nc1,caf\xe9,2026-10-30T10:00:00Z\nc2,"open quote\n'
    (data_dir / "feed.csv").write_bytes(content)
    set_mtime(data_dir / "feed.csv", _NOW)
    cut_offset = content.index(b"c2,")

    connections = FakeConnections()
    connections.add_local_folder("data_conn", data_dir)
    reader = FileSourceReader(connections)

    outcome = await reader.read_raw(
        project_id,
        source_id("file", "feed.csv", "data_conn"),
        cursor={},
        now=_NOW,
        mapping=_CSV_MAPPING,
    )
    assert outcome.last_error is None
    assert len(outcome.chunks) == 1
    chunk = outcome.chunks[0]
    assert chunk.new_offset == cut_offset
    assert chunk.content == content[:cut_offset].decode("utf-8", errors="replace")
    assert chunk.csv_header == ["id", "note", "time"]
    assert outcome.cursor["files"]["feed.csv"]["offset"] == cut_offset


async def test_last_line_without_newline_is_read_once_the_file_has_been_quiet(
    tmp_path: Path, project_id: str
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "results.jsonl"
    line1 = b'{"check_id": "chk_1", "origin": "agent", "ran_at": "2026-10-30T10:00:00Z", "fired": false, "category": "data_quality", "subject": "s", "detail": {}, "source_ref": "r"}\n'
    line2 = b'{"check_id": "chk_2", "origin": "agent", "ran_at": "2026-10-30T10:05:00Z", "fired": true, "category": "drift", "subject": "s", "detail": {}, "source_ref": "r"}'
    target.write_bytes(line1)

    connections = FakeConnections()
    connections.add_local_folder("data_conn", data_dir)
    reader = FileSourceReader(connections)
    s_id = source_id("file", "*.jsonl", "data_conn")

    out1 = await reader.read(project_id, s_id, cursor={}, now=_NOW)
    assert [r.check_id for _k, r in out1.records] == ["chk_1"]

    # A last line with no newline, unchanged but for less than the quiet time: held, as the
    # writer may still be writing it
    with target.open("ab") as f:
        f.write(line2)
    set_mtime(target, _NOW)
    out2 = await reader.read(project_id, s_id, cursor=out1.cursor, now=_NOW)
    assert out2.records == []
    out2 = await reader.read(project_id, s_id, cursor=out2.cursor, now=_NOW)
    assert out2.records == []

    # Unchanged for the quiet time: the writer has stopped, and the line is read once
    set_mtime(target, _NOW - timedelta(seconds=HELD_TAIL_QUIET_SECONDS))
    out3 = await reader.read(project_id, s_id, cursor=out2.cursor, now=_NOW)
    assert out3.last_error is None
    assert [(k, r.check_id) for k, r in out3.records] == [(f"results.jsonl:{len(line1)}", "chk_2")]
    assert out3.cursor["files"]["results.jsonl"]["offset"] == len(line1) + len(line2)

    out4 = await reader.read(project_id, s_id, cursor=out3.cursor, now=_NOW)
    assert out4.records == []
