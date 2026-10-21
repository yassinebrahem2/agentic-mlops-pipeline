import asyncio
import logging
import socket
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import uvicorn

from mlops_agents.cli import main
from mlops_agents.credentials.file_store import EncryptedFileStore


@pytest.fixture
def fake_serve(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()

    async def _serve(self: uvicorn.Server, sockets: list[Any] | None = None) -> None:
        self.started = True
        mock(self, sockets)
        await asyncio.sleep(0.01)

    monkeypatch.setattr(uvicorn.Server, "serve", _serve)
    return mock


@pytest.fixture
def mock_browser(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    import webbrowser

    mock = MagicMock()
    monkeypatch.setattr(webbrowser, "open", mock)
    return mock


def test_port_in_use_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 8765))
        sock.listen(1)
        with pytest.raises(SystemExit) as exc:
            main(["start"])

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert (
        "Port 8765 is in use. Start with --port and another number, for example --port 8766."
        in captured.err
    )


def test_server_without_env_vars_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MLOPS_AGENTS_PASSWORD", raising=False)
    monkeypatch.delenv("MLOPS_AGENTS_MASTER_KEY", raising=False)
    monkeypatch.delenv("MLOPS_AGENTS_ALLOWED_HOSTS", raising=False)

    with pytest.raises(SystemExit) as exc:
        main(["start", "--server"])

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "MLOPS_AGENTS_PASSWORD" in captured.err or "ConfigError" in captured.err


def test_local_start_prints_url_carrying_token_and_opens_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_serve: MagicMock,
    mock_browser: MagicMock,
) -> None:
    monkeypatch.setenv("MLOPS_AGENTS_HOME", str(tmp_path))

    main(["start"])

    captured = capsys.readouterr()
    assert "Open http://127.0.0.1:8765/#token=" in captured.out
    mock_browser.assert_called_once()
    opened_url = mock_browser.call_args[0][0]
    assert opened_url.startswith("http://127.0.0.1:8765/#token=")


def test_no_log_record_contains_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    fake_serve: MagicMock,
    mock_browser: MagicMock,
) -> None:
    monkeypatch.setenv("MLOPS_AGENTS_HOME", str(tmp_path))
    caplog.set_level(logging.DEBUG)

    main(["start"])

    captured = capsys.readouterr()
    token = captured.out.split("#token=")[1].strip()
    assert len(token) > 20
    for record in caplog.records:
        assert token not in record.getMessage()


def test_server_start_opens_no_browser_and_prints_no_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    fake_serve: MagicMock,
    mock_browser: MagicMock,
) -> None:
    monkeypatch.setenv("MLOPS_AGENTS_HOME", str(tmp_path))
    monkeypatch.setenv("MLOPS_AGENTS_PASSWORD", "super_secret_pw")
    monkeypatch.setenv("MLOPS_AGENTS_MASTER_KEY", "master_key_123")
    monkeypatch.setenv("MLOPS_AGENTS_ALLOWED_HOSTS", "app.example.com")
    caplog.set_level(logging.DEBUG)

    main(["start", "--server"])

    captured = capsys.readouterr()
    assert "Listening on 0.0.0.0:8765" in captured.out
    mock_browser.assert_not_called()
    assert "super_secret_pw" not in captured.out
    assert "super_secret_pw" not in captured.err
    for record in caplog.records:
        assert "super_secret_pw" not in record.getMessage()


def test_dev_mode_prints_and_opens_5173_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_serve: MagicMock,
    mock_browser: MagicMock,
) -> None:
    monkeypatch.setenv("MLOPS_AGENTS_HOME", str(tmp_path))

    main(["start", "--dev"])

    captured = capsys.readouterr()
    assert "Open http://localhost:5173/#token=" in captured.out
    mock_browser.assert_called_once()
    opened_url = mock_browser.call_args[0][0]
    assert opened_url.startswith("http://localhost:5173/#token=")


def test_no_browser_prints_link_and_never_calls_webbrowser_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_serve: MagicMock,
    mock_browser: MagicMock,
) -> None:
    monkeypatch.setenv("MLOPS_AGENTS_HOME", str(tmp_path))

    main(["start", "--no-browser"])

    captured = capsys.readouterr()
    assert "Open http://127.0.0.1:8765/#token=" in captured.out
    mock_browser.assert_not_called()


def test_server_dev_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["start", "--server", "--dev"])

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "--dev is for local mode only." in captured.err


def test_server_with_wrong_master_key_exits_2_no_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    keys_file = tmp_path / "keys.enc"
    EncryptedFileStore(keys_file, "original-key").set("key1", "val1")

    monkeypatch.setenv("MLOPS_AGENTS_HOME", str(tmp_path))
    monkeypatch.setenv("MLOPS_AGENTS_PASSWORD", "pwd")
    monkeypatch.setenv("MLOPS_AGENTS_MASTER_KEY", "different-wrong-key")
    monkeypatch.setenv("MLOPS_AGENTS_ALLOWED_HOSTS", "app.example.com")

    with pytest.raises(SystemExit) as exc:
        main(["start", "--server"])

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert len(captured.err.strip()) > 0


def test_backup_writes_app_db_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MLOPS_AGENTS_HOME", str(tmp_path))
    app_db = tmp_path / "app.db"
    import sqlite3

    conn = sqlite3.connect(app_db)
    conn.execute("CREATE TABLE test (x INTEGER)")
    conn.commit()
    conn.close()

    main(["backup"])

    captured = capsys.readouterr()
    backup_folder_str = captured.out.strip()
    backup_folder = Path(backup_folder_str)
    assert backup_folder.is_dir()
    assert (backup_folder / "app.db").exists()
