"""Constants for tracking sources, sync intervals, and query limits."""

from __future__ import annotations

from typing import Final

SYNC_INTERVAL_KEY: Final[str] = "tracking_sync_interval_s"
DEFAULT_SYNC_INTERVAL_S: Final[int] = 60  # I16: one minute, a project setting
MIN_SYNC_INTERVAL_S: Final[int] = 15
QUERY_LIMIT_MAX: Final[int] = 500
QUERY_LIMIT_DEFAULT: Final[int] = 50
ERROR_MAX_CHARS: Final[int] = 500
PRODUCTION_ALIAS: Final[str] = "production"
PRODUCTION_ENVIRONMENT: Final[str] = "production"
