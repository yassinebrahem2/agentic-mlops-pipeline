# benchmarks

Developer benchmarks for `mlops-agents`: a developer tool outside the product that scores the app against known problems through the product's own verdict routes and reads the numbers back from its Stats.

## Use

Its own uv project; run from `bench/benchmarks/`:

```bash
uv sync
uv run bench --help
uv run pytest
uv run ruff check
uv run ruff format --check
uv run pyright
```

## Structure

- `src/benchmarks/client.py`: `BenchClient`, `sign_in`, `in_process`
- `src/benchmarks/dates.py`: `DayStarts`, `business_date`
- `src/benchmarks/cli.py`: CLI dispatcher for `bench`
