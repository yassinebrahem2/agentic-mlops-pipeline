import asyncio
import errno
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
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


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
    free_port: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", free_port))
        sock.listen(1)
        with pytest.raises(SystemExit) as exc:
            main(["start", "--port", str(free_port)])

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert (
        f"Port {free_port} is in use. Start with --port and another number, for example --port {free_port + 1}."
        in captured.err
    )


def test_port_in_use_with_reuseaddr_occupying_socket_exits_2(
    free_port: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", free_port))
        sock.listen(1)
        with pytest.raises(SystemExit) as exc:
            main(["start", "--port", str(free_port)])

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert (
        f"Port {free_port} is in use. Start with --port and another number, for example --port {free_port + 1}."
        in captured.err
    )


def test_other_oserror_on_bind_exits_2_plain_message(
    free_port: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _fake_bind(self: socket.socket, address: Any) -> None:
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(socket.socket, "bind", _fake_bind)
    with pytest.raises(SystemExit) as exc:
        main(["start", "--no-browser", "--port", str(free_port)])

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "Invalid argument" in captured.err


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
    assert (
        "Server mode needs these environment variables: MLOPS_AGENTS_PASSWORD, MLOPS_AGENTS_MASTER_KEY, MLOPS_AGENTS_ALLOWED_HOSTS. Set them and start again."
        in captured.err
    )


def test_local_start_prints_url_carrying_token_and_opens_browser(
    tmp_path: Path,
    free_port: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_serve: MagicMock,
    mock_browser: MagicMock,
) -> None:
    monkeypatch.setenv("MLOPS_AGENTS_HOME", str(tmp_path))

    main(["start", "--port", str(free_port)])

    captured = capsys.readouterr()
    assert f"Open http://127.0.0.1:{free_port}/#token=" in captured.out
    mock_browser.assert_called_once()
    opened_url = mock_browser.call_args[0][0]
    assert opened_url.startswith(f"http://127.0.0.1:{free_port}/#token=")


def test_no_log_record_contains_token(
    tmp_path: Path,
    free_port: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    fake_serve: MagicMock,
    mock_browser: MagicMock,
) -> None:
    monkeypatch.setenv("MLOPS_AGENTS_HOME", str(tmp_path))
    caplog.set_level(logging.DEBUG)

    main(["start", "--port", str(free_port)])

    captured = capsys.readouterr()
    token = captured.out.split("#token=")[1].strip()
    assert len(token) > 20
    for record in caplog.records:
        assert token not in record.getMessage()


def test_server_start_opens_no_browser_and_prints_no_password(
    tmp_path: Path,
    free_port: int,
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

    main(["start", "--server", "--port", str(free_port)])

    captured = capsys.readouterr()
    assert f"Listening on 0.0.0.0:{free_port}" in captured.out
    mock_browser.assert_not_called()
    assert "super_secret_pw" not in captured.out
    assert "super_secret_pw" not in captured.err
    for record in caplog.records:
        assert "super_secret_pw" not in record.getMessage()


def test_dev_mode_prints_and_opens_5173_url(
    tmp_path: Path,
    free_port: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_serve: MagicMock,
    mock_browser: MagicMock,
) -> None:
    monkeypatch.setenv("MLOPS_AGENTS_HOME", str(tmp_path))

    main(["start", "--dev", "--port", str(free_port)])

    captured = capsys.readouterr()
    assert "Open http://localhost:5173/#token=" in captured.out
    mock_browser.assert_called_once()
    opened_url = mock_browser.call_args[0][0]
    assert opened_url.startswith("http://localhost:5173/#token=")

    server_instance = fake_serve.call_args[0][0]
    host_rules = server_instance.config.app.state.host_rules
    assert host_rules.origin_allowed("http://localhost:5173")
    assert host_rules.origin_allowed("http://127.0.0.1:5173")
    assert not host_rules.origin_allowed("http://evil.example.com:5173")


def test_no_browser_prints_link_and_never_calls_webbrowser_open(
    tmp_path: Path,
    free_port: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_serve: MagicMock,
    mock_browser: MagicMock,
) -> None:
    monkeypatch.setenv("MLOPS_AGENTS_HOME", str(tmp_path))

    main(["start", "--no-browser", "--port", str(free_port)])

    captured = capsys.readouterr()
    assert f"Open http://127.0.0.1:{free_port}/#token=" in captured.out
    mock_browser.assert_not_called()


def test_start_ctrl_c_exits_quietly(
    tmp_path: Path,
    free_port: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MLOPS_AGENTS_HOME", str(tmp_path))

    async def _sigint_serve(self: uvicorn.Server, sockets: list[Any] | None = None) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(uvicorn.Server, "serve", _sigint_serve)
    code = main(["start", "--no-browser", "--port", str(free_port)])
    assert code == 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err


def test_server_dev_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["start", "--server", "--dev"])

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "--dev is for local mode only." in captured.err


def test_server_with_wrong_master_key_exits_2_no_traceback(
    tmp_path: Path,
    free_port: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    keys_file = tmp_path / "keys.enc"
    EncryptedFileStore(keys_file, "original-key").set("key1", "val1")

    monkeypatch.setenv("MLOPS_AGENTS_HOME", str(tmp_path))
    monkeypatch.setenv("MLOPS_AGENTS_PASSWORD", "pwd")
    monkeypatch.setenv("MLOPS_AGENTS_MASTER_KEY", "different-wrong-key")
    monkeypatch.setenv("MLOPS_AGENTS_ALLOWED_HOSTS", "app.example.com")

    with pytest.raises(SystemExit) as exc:
        main(["start", "--server", "--port", str(free_port)])

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "MLOPS_AGENTS_MASTER_KEY does not unlock keys.enc." in captured.err


def test_backup_ctrl_c_exits_quietly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MLOPS_AGENTS_HOME", str(tmp_path))

    def _sigint(*args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr("mlops_agents.cli.snapshot_all", _sigint)
    code = main(["backup"])
    assert code == 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err


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
