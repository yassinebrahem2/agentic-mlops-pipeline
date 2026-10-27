"""Downloading the source CSV once, from a pinned URL, checked against a pinned SHA-256."""

import hashlib
import urllib.request
from pathlib import Path

# TidyTuesday 2020-02-11 copy of Hotel Booking Demand (Antonio, Almeida and Nunes, 2019),
# pinned to a commit of rfordatascience/tidytuesday so the bytes behind the hash never change.
SOURCE_URL = (
    "https://raw.githubusercontent.com/rfordatascience/tidytuesday/"
    "d75aaa0d31596ad6487ae0db20138067d301e031/data/2020/2020-02-11/hotels.csv"
)
SOURCE_SHA256 = "7c2ae42a7353905ea136e5c2287f17c92c5435826598bfbb8491c6f0c7b1fc06"
SOURCE_FILE = "hotels.csv"

_CHUNK = 1 << 20
_TIMEOUT_SECONDS = 120


class FetchError(Exception):
    """The source could not be downloaded, or its bytes are not the pinned ones."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(dest_dir: Path, url: str = SOURCE_URL, sha256: str = SOURCE_SHA256) -> Path:
    """Return `dest_dir/hotels.csv`, downloading it unless a copy with the right hash is there."""
    target = dest_dir / SOURCE_FILE
    if target.exists():
        if _sha256(target) == sha256:
            return target
        # A damaged cache must not survive a failed re-download and be read as the source.
        target.unlink()

    dest_dir.mkdir(parents=True, exist_ok=True)
    partial = dest_dir / f"{SOURCE_FILE}.part"
    digest = hashlib.sha256()
    try:
        # The URL is the pinned constant; tests inject a localhost one.
        with (
            urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS) as response,  # noqa: S310
            partial.open("wb") as out,
        ):
            while chunk := response.read(_CHUNK):
                digest.update(chunk)
                out.write(chunk)
    except OSError as error:
        partial.unlink(missing_ok=True)
        raise FetchError(f"downloading {url} failed: {error}") from error

    actual = digest.hexdigest()
    if actual != sha256:
        partial.unlink()
        raise FetchError(f"{url} has SHA-256 {actual}, expected {sha256}; nothing was kept")
    partial.replace(target)
    return target
