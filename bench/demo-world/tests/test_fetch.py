import http.client
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


@pytest.fixture
def cut_short_url() -> Iterator[str]:
    """A localhost URL whose chunked body stops in the middle of a chunk."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            # One whole chunk, then a chunk that announces 4096 bytes and sends 10.
            self.wfile.write(b"10\r\n" + b"a" * 16 + b"\r\n" + b"1000\r\n" + b"b" * 10)
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/hotels.csv"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_a_body_cut_short_fails_and_leaves_nothing(cut_short_url: str) -> None:
    with pytest.raises(fetch.FetchError) as raised:
        fetch.fetch(paths.source_dir(), url=cut_short_url, sha256="0" * 64)

    assert isinstance(raised.value.__cause__, http.client.IncompleteRead)
    assert list(paths.source_dir().iterdir()) == []


def test_the_pinned_url_names_a_commit_not_a_branch() -> None:
    # A branch can move; a commit SHA pins the bytes the hash describes.
    commit = fetch.SOURCE_URL.split("/tidytuesday/")[1].split("/")[0]
    assert len(commit) == 40
    assert all(c in "0123456789abcdef" for c in commit)
    assert len(fetch.SOURCE_SHA256) == 64
