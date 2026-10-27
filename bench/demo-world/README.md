# demo-world

The world around the demo company, a developer tool outside the product. The company (the demo repository `yassinebrahem2/hotel-cancellation-demo`) scores hotel bookings for cancellation; `demo-world` plays everything around it: it downloads the source data, gives the company its history, and (in later tasks) plays time day by day, injects problems and keeps the evaluation's answer key. The two share no code: the data folder layout in `docs/superpowers/plans/2026-10-27-demo-overview.md` is their interface.

## Use

Its own uv project; run from `bench/demo-world/`:

```
uv sync
uv run demo-world fetch                       # download the source CSV once, checked against its pinned SHA-256
uv run demo-world backfill --until 2016-01-01 # write the history before that day into an empty data folder
uv run pytest
uv run ruff check
uv run ruff format --check
```

Folders, each overridable by an environment variable, default under `~/hotel-demo/`:

| Variable | Default | Holds |
|---|---|---|
| `HOTEL_DEMO_SOURCE` | `source/` | the downloaded `hotels.csv`, shared by every run |
| `HOTEL_DEMO_DATA` | `data/` | the company's production data: `bookings/` and `labels/`, one parquet file per business date |
| `HOTEL_DEMO_WORLD` | `world/` | the world's own state, never inside the data folder |

`backfill` writes the bookings of every booking date before `--until` (long-lead bookings made before the first arrivals included) and the labels of every release date before it. A label is released on the cancellation date if the booking was cancelled, otherwise on the arrival date, and never before the booking date. It refuses a data folder that already has files.

## Data

Hotel Booking Demand, by Nuno Antonio, Ana de Almeida and Luis Nunes, *Data in Brief* 22 (2019), [doi:10.1016/j.dib.2018.11.126](https://doi.org/10.1016/j.dib.2018.11.126), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). `fetch` downloads the copy published by TidyTuesday (2020-02-11) from a URL pinned to one commit of `rfordatascience/tidytuesday`. The data is re-sliced, not redistributed as is: the world converts its missing-value markers to nulls, derives booking and release dates, and splits it into one file per business date. Neither this repository nor the demo repository commits the CSV.
