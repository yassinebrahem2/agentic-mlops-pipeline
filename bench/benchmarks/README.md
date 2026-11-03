# benchmarks

Developer benchmarks for `mlops-agents`: a developer tool outside the product that scores the app against known problems through the product's own verdict routes and reads the numbers back from its Stats.

## Use

The benchmark harness stores cache, runs, and artifacts under `$BENCH_HOME`, which defaults to `~/bench-home`.

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

## The agents run

Runbook, in order:
1. `new-run`, then `backfill`.
2. `up --hold-before-start` with the pace, the merge delay and `scenarios/agents-short.toml`.
3. The app on a dedicated `MLOPS_AGENTS_HOME`.
4. `bench agents-run`, with the same pace and scenario as `up` and `MLOPS_AGENTS_ACCESS_TOKEN` set to the app's access token (`export MLOPS_AGENTS_ACCESS_TOKEN=<token>`).
5. While it runs: approve Onboarding's proposals, merge the first-audit PRs by hand, press Try again on every stall, give no verdict or label, keep the machine awake.

## Datasets

### ULB Credit Card Fraud
- Source: OpenML dataset 1597 (`creditcard` v1, `https://data.openml.org/datasets/0000/1597/dataset_1597.pq`).
- Licence: OpenML lists "Public"; original dataset page lists Database Contents License (DbCL) 1.0.
- Citation: Andrea Dal Pozzolo, Olivier Caelen, Reid A. Johnson and Gianluca Bontempi. *Calibrating Probability with Undersampling for Unbalanced Classification*. IEEE Symposium on Computational Intelligence and Data Mining (CIDM), 2015.

