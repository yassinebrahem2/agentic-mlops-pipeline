from datetime import date
from pathlib import Path

# A hand-written slice shaped like the source CSV (same header and markers).
FIXTURE_CSV = Path(__file__).parent / "fixtures" / "hotels.csv"
# The fixture's history slice arrived before this day; its five booking days start on it.
FIXTURE_START = date(2016, 1, 1)
