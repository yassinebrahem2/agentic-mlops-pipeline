"""Constants for monitoring results (I22)."""

from typing import Final

QUERY_LIMIT_MAX: Final[int] = 500
DETAIL_MAX_BYTES: Final[int] = 32_768
# A last record without its newline is read only once its file has been unchanged this long:
# a buffered writer can flush part of a record and stay idle for a whole collection interval.
HELD_TAIL_QUIET_SECONDS: Final[int] = 300
