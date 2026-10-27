import hashlib
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from demo_world import fetch, paths
from tests.fixture import FIXTURE_CSV


@dataclass
class Served:
    url: str
    sha256: str
    requests: list[str] = field(default_factory=list[str])


@pytest.fixture
def served() -> Iterator[Served]:
    """Serve the fixture CSV on localhost, recording every request."""
    # Hash the bytes on disk: a Windows checkout may have converted the CSV's line endings.
    sha256 = hashlib.sha256(FIXTURE_CSV.read_bytes()).hexdigest()
    requests: list[str] = []

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            super().do_GET()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(Handler, directory=str(FIXTURE_CSV.parent))
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        port = server.server_address[1]
        yield Served(f"http://127.0.0.1:{port}/{FIXTURE_CSV.name}", sha256, requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


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
