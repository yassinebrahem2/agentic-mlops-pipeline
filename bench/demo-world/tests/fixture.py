from dataclasses import dataclass
from datetime import date
from pathlib import Path

# A hand-written slice shaped like the source CSV (same header and markers).
FIXTURE_CSV = Path(__file__).parent / "fixtures" / "hotels.csv"
# The fixture's history slice arrived before this day; its five booking days start on it.
FIXTURE_START = date(2016, 1, 1)
FIXTURE_END = date(2016, 1, 5)
# The scenario file the world ships, read where it lives in the project.
DEFAULT_SCENARIO = Path(__file__).parents[1] / "scenarios" / "default.toml"


@dataclass
class Served:
    """The fixture CSV served on localhost: its URL, its hash, the paths requested."""

    url: str
    sha256: str
    requests: list[str]
