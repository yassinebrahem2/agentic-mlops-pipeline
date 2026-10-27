import shutil

import pytest

from demo_world import cli, fetch, paths
from tests.fixture import FIXTURE_CSV, Served


def _place_source() -> None:
    paths.source_dir().mkdir(parents=True)
    shutil.copyfile(FIXTURE_CSV, paths.source_dir() / fetch.SOURCE_FILE)


def test_fetch_downloads_the_pinned_source(
    served: Served, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(fetch, "SOURCE_URL", served.url)
    monkeypatch.setattr(fetch, "SOURCE_SHA256", served.sha256)

    assert cli.main(["fetch"]) == 0

    assert (paths.source_dir() / "hotels.csv").read_bytes() == FIXTURE_CSV.read_bytes()
    assert str(paths.source_dir() / "hotels.csv") in capsys.readouterr().out


def test_fetch_reports_a_wrong_hash(
    served: Served, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(fetch, "SOURCE_URL", served.url)
    monkeypatch.setattr(fetch, "SOURCE_SHA256", "0" * 64)

    assert cli.main(["fetch"]) == 1

    assert served.sha256 in capsys.readouterr().err


def test_backfill_writes_the_history(capsys: pytest.CaptureFixture[str]) -> None:
    _place_source()

    assert cli.main(["backfill", "--until", "2016-01-01"]) == 0

    assert len(list((paths.data_dir() / "bookings").iterdir())) == 10
    assert len(list((paths.data_dir() / "labels").iterdir())) == 8
    assert "10 bookings files" in capsys.readouterr().out


def test_backfill_without_the_source_says_to_fetch_it(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["backfill", "--until", "2016-01-01"]) == 1

    assert "demo-world fetch" in capsys.readouterr().err
    assert not paths.data_dir().exists()


def test_backfill_refuses_a_data_folder_with_files(capsys: pytest.CaptureFixture[str]) -> None:
    _place_source()
    paths.data_dir().mkdir(parents=True)
    (paths.data_dir() / "notes.txt").write_text("x", encoding="utf-8")

    assert cli.main(["backfill", "--until", "2016-01-01"]) == 1

    assert "already has files" in capsys.readouterr().err


def test_until_must_be_a_date() -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["backfill", "--until", "January"])

    assert raised.value.code == 2
