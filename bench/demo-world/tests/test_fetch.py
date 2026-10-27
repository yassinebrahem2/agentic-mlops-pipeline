import pytest

from demo_world import fetch, paths
from tests.fixture import FIXTURE_CSV, Served


def test_fetch_writes_the_source_when_the_hash_matches(served: Served) -> None:
    written = fetch.fetch(paths.source_dir(), url=served.url, sha256=served.sha256)

    assert written == paths.source_dir() / "hotels.csv"
    assert written.read_bytes() == FIXTURE_CSV.read_bytes()
    assert sorted(p.name for p in paths.source_dir().iterdir()) == ["hotels.csv"]


def test_fetch_downloads_once_then_uses_the_cache(served: Served) -> None:
    fetch.fetch(paths.source_dir(), url=served.url, sha256=served.sha256)
    fetch.fetch(paths.source_dir(), url=served.url, sha256=served.sha256)

    assert len(served.requests) == 1


def test_a_wrong_hash_fails_naming_both_hashes_and_leaves_nothing(served: Served) -> None:
    wrong = "0" * 64

    with pytest.raises(fetch.FetchError) as raised:
        fetch.fetch(paths.source_dir(), url=served.url, sha256=wrong)

    assert wrong in str(raised.value)
    assert served.sha256 in str(raised.value)
    assert list(paths.source_dir().iterdir()) == []


def test_a_cached_file_with_the_wrong_hash_is_downloaded_again(served: Served) -> None:
    paths.source_dir().mkdir(parents=True)
    (paths.source_dir() / "hotels.csv").write_text("damaged", encoding="utf-8")

    written = fetch.fetch(paths.source_dir(), url=served.url, sha256=served.sha256)

    assert written.read_bytes() == FIXTURE_CSV.read_bytes()
    assert len(served.requests) == 1


def test_a_failed_download_fails_and_leaves_nothing(served: Served) -> None:
    with pytest.raises(fetch.FetchError):
        fetch.fetch(paths.source_dir(), url=served.url + ".missing", sha256=served.sha256)

    assert list(paths.source_dir().iterdir()) == []


def test_the_pinned_url_names_a_commit_not_a_branch() -> None:
    # A branch can move; a commit SHA pins the bytes the hash describes.
    commit = fetch.SOURCE_URL.split("/tidytuesday/")[1].split("/")[0]
    assert len(commit) == 40
    assert all(c in "0123456789abcdef" for c in commit)
    assert len(fetch.SOURCE_SHA256) == 64
