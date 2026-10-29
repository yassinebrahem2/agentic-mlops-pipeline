# demo-world

The world around the demo company, a developer tool outside the product. The company (the demo repository `yassinebrahem2/hotel-cancellation-demo`) scores hotel bookings for cancellation; `demo-world` plays everything around it: it downloads the source data, gives the company its history, plays time day by day, injects problems at known dates and keeps the evaluation's answer key, and runs the company's scheduled jobs each day. The two share no code: the data folder layout and the company's CLI in `docs/superpowers/plans/2026-10-27-demo-overview.md` are their interface.

## Use

Its own uv project; run from `bench/demo-world/`:

```
uv sync
uv run demo-world fetch                       # download the source CSV once, checked against its pinned SHA-256
uv run demo-world backfill --until 2016-01-01 # write the history before that day into an empty data folder
uv run demo-world simulate --start 2016-01-01 --end 2017-03-31 --seconds-per-day 120 --scenario scenarios/default.toml
uv run pytest
uv run ruff check
uv run ruff format --check
```

Folders, each overridable by an environment variable, default under `~/hotel-demo/`:

| Variable | Default | Holds |
|---|---|---|
| `HOTEL_DEMO_SOURCE` | `source/` | the downloaded `hotels.csv`, shared by every run |
| `HOTEL_DEMO_DATA` | `data/` | the company's production data: `bookings/` and `labels/`, one parquet file per business date |
| `HOTEL_DEMO_WORLD` | `world/` | the world's own folder, never inside the data folder: `state.json`, `answer_key.jsonl`, `planted_flaws.json` |
| `HOTEL_DEMO_MLFLOW` | `mlflow/` | the company's MLflow store, passed to every company command |
| `HOTEL_DEMO_COMPANY` | none | a checkout of the demo repository; without it `simulate` writes the files only |

`backfill` writes the bookings of every booking date before `--until` (long-lead bookings made before the first arrivals included) and the labels of every release date before it. A label is released on the cancellation date if the booking was cancelled, otherwise on the arrival date, and never before the booking date. It refuses a data folder that already has files.

A backfill that failed midway leaves a non-empty data folder, which the next run refuses: delete that folder and run `backfill` again.

`simulate` plays every calendar day from `--start` to `--end` (both included) as a business date, after the history: each day it writes that day's bookings file, then that day's labels file (the labels released that day); a day with no rows gets no file. `--seconds-per-day` paces it (each day starts that many seconds after the previous one did; 0 runs as fast as it can). `state.json` records the first day played and the last day done, written once the whole day is; a stopped or crashed run started again resumes the day after it and replays a half-done day, writing the world's files identically (the company's own `jobs` and `monitoring` lines of a replayed job are appended again). A `--start` after that day is refused, since it would skip days.

With `HOTEL_DEMO_COMPANY` set, the world runs the company as its ops host would, from the company's own environment (`uv run --project <checkout> hotel-demo ...`, with `HOTEL_DEMO_DATA` and `HOTEL_DEMO_MLFLOW` set to the world's folders, and `MLFLOW_TRACKING_URI` and the other variables passed through):
- before each day, `git pull --ff-only` in the checkout, so merged PRs take effect; a failing pull is logged and the day goes on with what is checked out;
- before the first day's files, if the company has no `production` model, `train --cutoff <first day>`, then `evaluate` and `deploy` of the version `train` prints last. Whether one exists is asked of MLflow from the company's environment (`mlflow artifacts download` of `models:/hotel-cancellation@production`, against `MLFLOW_TRACKING_URI` or the company's documented default store); when MLflow cannot say, nothing is trained;
- after the day's files, every job of the checkout's `ops/schedule.toml` due that day, in file order, with `{date}` in its command replaced by the day. A job is due every `every` business days counted from the first day the world played. The file is read again each day, so a job added between days runs from its next due day.

A failing command is logged (command, exit code, the end of its output) and the loop goes on; the log goes to stderr.

`--scenario` names a TOML file of problems to inject; without it nothing is injected. `scenarios/default.toml` holds one problem of each kind spread over 2016, and the natural shift the evaluation scores. Each `[[injection]]` has a `kind`, a `start` and an `end` (business dates, both included), and changes the bookings files dated in that window:

| `kind` | Keys | Effect |
|---|---|---|
| `null_spike` | `columns`, `share` (above 0, at most 1) | sets that share of each day's rows to null in each column, rounded up |
| `schema_break` | `column`, and `rename_to` or `drop = true` | renames or drops the column |
| `out_of_range` | `column` (a float column), `factor` | multiplies the column |
| `score_failure` | none | with a company, the day's files are delivered after its jobs ran, so its scoring fails on a missing bookings file |
| `ci_failure` | none | recorded in the answer key only (acted on later) |

Injections that change files must not overlap. Rows are picked from a digest of the kind, column, day and booking, so a rerun writes the same files. Each `[[natural_shift]]` (`columns`, `start`, `end`) is not injected, only listed for scoring.

When the loop reaches a problem's first day in the run, it appends one line per column (or one with `column` null) to `answer_key.jsonl`: `{kind, category, column, start, end}`, with `category` one of `data_quality`, `drift`, `model_performance`, `operational`; a line already there is never added again. `planted_flaws.json` lists the flaws planted in the company's setup by design (`id`, `category`, `where`, `what`), rewritten at every start. Neither is ever written into the data folder, and `simulate` refuses a world folder inside it.

## Data

Hotel Booking Demand, by Nuno Antonio, Ana de Almeida and Luis Nunes, *Data in Brief* 22 (2019), [doi:10.1016/j.dib.2018.11.126](https://doi.org/10.1016/j.dib.2018.11.126), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). `fetch` downloads the copy published by TidyTuesday (2020-02-11) from a URL pinned to one commit of `rfordatascience/tidytuesday`. The data is re-sliced, not redistributed as is: the world converts its missing-value markers to nulls, derives booking and release dates, and splits it into one file per business date. Neither this repository nor the demo repository commits the CSV.
