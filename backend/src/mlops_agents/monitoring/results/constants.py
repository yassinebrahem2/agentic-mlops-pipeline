"""Constants for monitoring results (I22)."""

from typing import Final

QUERY_LIMIT_MAX: Final[int] = 500
DETAIL_MAX_BYTES: Final[int] = 32_768
# A last record without its newline is read only once its file has been unchanged this long:
# a buffered writer can flush part of a record and stay idle for a whole collection interval.
HELD_TAIL_QUIET_SECONDS: Final[int] = 300
# A code parser is given a file in pieces of at most this many bytes, each its own run: the
# sandbox keeps about 30,000 characters of a run's output (HEAD_CAPTURE_CHARS +
# TAIL_CAPTURE_CHARS in sandbox/docker.py), and a run whose output was cut fails. A record's
# JSON is usually a small multiple of its input line, so 8 KiB leaves room for a few times that.
# The script decides how much it prints, so CodeParserRunner runs a chunk whose output was still
# cut again in smaller pieces.
CODE_PARSER_CHUNK_BYTES: Final[int] = 8_192
