"""The one source of every cap and version of the data summaries."""

from typing import Final

SAMPLE_ROWS: Final = 20
TEXT_CUT: Final = 80
MAX_COLUMNS: Final = 50
MAX_TABLES: Final = 20
TOP_VALUES: Final = 5
LISTED_FILES: Final = 5
LISTED_TABLE_NAMES: Final = 20
SUMMARY_FORMAT_VERSION: Final = 4
DATA_SUMMARY_MAX_CHARS: Final = 20_000
CACHE_CAP_BYTES: Final = 500 * 1024 * 1024
PROFILE_TIMEOUT_S: Final = 30
HOOK_BUDGET_S: Final = 60
# Each locked DuckDB connection's share of the app's process. Nothing spills to disk
# (Decision #431): a profile that needs more memory keeps what finished, marked incomplete
# (Decision #432).
DUCKDB_MEMORY_LIMIT: Final = "1GiB"
DUCKDB_THREADS: Final = 2
# Profiles running at once, so the app's DuckDB memory stays near this many limits (#432).
MAX_CONCURRENT_PROFILES: Final = 2
# A text column with at least this share of distinct values among its rows gets no top
# values: they are noise, and their grouping is what runs out of memory (Decision #432).
NEARLY_UNIQUE_SHARE: Final = 0.95
