"""The one source of every cap and version of the data summaries."""

from typing import Final

SAMPLE_ROWS: Final = 20
TEXT_CUT: Final = 80
MAX_COLUMNS: Final = 50
MAX_TABLES: Final = 20
TOP_VALUES: Final = 5
LISTED_FILES: Final = 5
LISTED_TABLE_NAMES: Final = 20
SUMMARY_FORMAT_VERSION: Final = 2
DATA_SUMMARY_MAX_CHARS: Final = 20_000
CACHE_CAP_BYTES: Final = 500 * 1024 * 1024
PROFILE_TIMEOUT_S: Final = 30
HOOK_BUDGET_S: Final = 60
