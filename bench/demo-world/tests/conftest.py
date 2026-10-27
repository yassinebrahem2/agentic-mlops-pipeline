from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every folder, and the home folder itself, at the test's temporary folder."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOTEL_DEMO_SOURCE", str(tmp_path / "source"))
    monkeypatch.setenv("HOTEL_DEMO_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("HOTEL_DEMO_WORLD", str(tmp_path / "world"))
    return tmp_path
