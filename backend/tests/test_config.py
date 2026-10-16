from pathlib import Path

import pytest

from mlops_agents.config import ConfigError, RunMode, load_settings


def test_local_defaults_bind_loopback(tmp_path: Path) -> None:
    s = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(tmp_path)})
    assert s.host == "127.0.0.1"
    assert s.port == 8765
    assert s.data_dir == tmp_path
    assert s.password is None
    assert s.master_key is None


def test_local_ignores_server_secrets(tmp_path: Path) -> None:
    s = load_settings(
        RunMode.LOCAL,
        env={"MLOPS_AGENTS_HOME": str(tmp_path), "MLOPS_AGENTS_PASSWORD": "pw"},
    )
    assert s.password is None


def test_server_requires_password_master_key_and_hosts(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as err:
        load_settings(RunMode.SERVER, env={"MLOPS_AGENTS_HOME": str(tmp_path)})
    message = str(err.value)
    assert "MLOPS_AGENTS_PASSWORD" in message
    assert "MLOPS_AGENTS_MASTER_KEY" in message
    assert "MLOPS_AGENTS_ALLOWED_HOSTS" in message


def test_server_settings(tmp_path: Path) -> None:
    s = load_settings(
        RunMode.SERVER,
        port=9000,
        env={
            "MLOPS_AGENTS_HOME": str(tmp_path),
            "MLOPS_AGENTS_PASSWORD": "pw",
            "MLOPS_AGENTS_MASTER_KEY": "mk",
            "MLOPS_AGENTS_ALLOWED_HOSTS": "agents.example.com, other.example.com",
            "MLOPS_AGENTS_TRUSTED_PROXIES": "10.0.0.1",
        },
    )
    assert s.host == "0.0.0.0"
    assert s.port == 9000
    assert s.allowed_hosts == ("agents.example.com", "other.example.com")
    assert s.trusted_proxies == ("10.0.0.1",)


def test_default_data_dir_is_not_roaming() -> None:
    s = load_settings(RunMode.LOCAL, env={})
    assert "mlops-agents" in str(s.data_dir)
    assert "Roaming" not in str(s.data_dir)


@pytest.mark.parametrize("port", [0, -1, 65536, 100000])
def test_invalid_port_raises_config_error(port: int) -> None:
    with pytest.raises(ConfigError, match="Port must be between 1 and 65535"):
        load_settings(RunMode.LOCAL, port=port)


def test_repr_hides_secrets(tmp_path: Path) -> None:
    s = load_settings(
        RunMode.SERVER,
        env={
            "MLOPS_AGENTS_HOME": str(tmp_path),
            "MLOPS_AGENTS_PASSWORD": "pw-secret",
            "MLOPS_AGENTS_MASTER_KEY": "mk-secret",
            "MLOPS_AGENTS_ACCESS_TOKEN": "tok-secret",
            "MLOPS_AGENTS_ALLOWED_HOSTS": "agents.example.com",
        },
    )
    text = repr(s)
    for secret in ("pw-secret", "mk-secret", "tok-secret"):
        assert secret not in text
