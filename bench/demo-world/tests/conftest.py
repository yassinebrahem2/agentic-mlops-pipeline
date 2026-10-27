from datetime import date
from pathlib import Path

import pytest

FIXTURE_CSV = Path(__file__).parent / "fixtures" / "hotels.csv"
# The fixture's history slice arrived before this day; its five booking days start on it.
FIXTURE_START = date(2016, 1, 1)


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
