# Demo Project — Plans Overview

> **Draft.** Missing: an advisor round with no blocking issues.

The demo is a separate repository, `yassinebrahem2/hotel-cancellation-demo`, that simulates a hotel group's live ML system, so every feature of the app can be shown on it and the evaluation can measure the agents against the company's own monitoring. Facts and the story: `project_knowledge.md` (Data access; the demo entries of 2026-10-27).

**Story.** A hotel group (a city hotel and a resort) scores every new booking at booking time with the probability that it will be cancelled. The revenue team adds up the expected cancellations per arrival date and overbooks by that much. The goal is to fill rooms without turning away guests who hold a reservation. The company tracks its models in MLflow and runs simple monitoring of its own (the "legacy monitoring"), which is the app's baseline.

**Where things live.** Plans, plan issues and task issues: this repository (`docs/superpowers/plans/`, the project board, milestone "Demo project"). Code, branches, CI and PRs: the demo repository. The rules of `AGENTS.md` here apply there in full; the demo repository's own `AGENTS.md` says so and lists its commands. The lead reviews and merges the demo's PRs the same way.

**Tech stack.** Python 3.12, uv, pandas, pyarrow, LightGBM, scikit-learn (metrics and calibration), scipy (the legacy drift test), MLflow (server and client), pytest, ruff. No web framework: scoring is a batch job, as it is for overbooking.

## Global constraints

- **Data.** Hotel Booking Demand (Antonio, Almeida and Nunes, *Data in Brief* 22, 2019, doi:10.1016/j.dib.2018.11.126), CC BY 4.0. The CSV is never committed: `hotel-demo fetch` downloads it from a pinned URL, checks a pinned SHA-256 and writes it under the source folder. The README credits the authors, links the licence and states that the data is re-sliced.
- **Folders.** All outside the repository, each overridable by an environment variable, defaults under `~/hotel-demo/`:
  - `HOTEL_DEMO_SOURCE` (`source/`): the downloaded CSV. Only `fetch` and the simulator read it.
  - `HOTEL_DEMO_DATA` (`data/`): the company's production data, what an app data connection points at.
  - `HOTEL_DEMO_SIM` (`sim/`): the simulator's own state and the answer key. Never inside the data folder.
  - `HOTEL_DEMO_MLFLOW` (`mlflow/`): MLflow's SQLite store and artifacts.
- **Business dates.** Every file in the data folder carries its business date (the simulated day) in its name, `YYYY-MM-DD`. Nothing in the data depends on the wall clock.
- **No leaks.** The model never uses columns known only after booking: `reservation_status`, `reservation_status_date`, `assigned_room_type`, `booking_changes`, `days_in_waiting_list`, `required_car_parking_spaces`, `total_of_special_requests`, `is_canceled`. The list lives in one constant.
- **Labels arrive late.** A booking's label (`is_canceled`) is released on its cancellation date (`reservation_status_date`) if cancelled, otherwise on its arrival date.
- **One source.** Paths, the leak list, the feature list, the model name (`hotel-cancellation`) and the production alias (`production`) each have one definition in the package.

## Data folder layout (the interface between the plans)

```
data/
  bookings/YYYY-MM-DD.parquet      new bookings made that day: booking_id + the source columns minus the label fields
  predictions/YYYY-MM-DD.parquet   booking_id, business_date, model_version, p_cancel
  labels/YYYY-MM-DD.parquet        booking_id, is_canceled, label_date  (labels released that day)
  monitoring/YYYY-MM-DD.json       the legacy monitoring's results for that day
  jobs/YYYY-MM-DD.jsonl            one line per job run: job, business_date, started, ended, status, error
```

`booking_id` is the source row number, stable across runs.

## Plans

| Plan | Branch (demo repo) | Covers | Consumes | Produces | Starts |
|---|---|---|---|---|---|
| demo-pipeline | `plan/demo-pipeline` | Package skeleton, `fetch`, prepare, train, evaluate, register in MLflow, batch scoring | none | `hotel_demo.paths`, `hotel_demo.features`, `train(cutoff)`, `score(business_date)`, the bookings and predictions files | now |
| demo-simulator | `plan/demo-simulator` | The simulator, injected problems, the answer key, legacy monitoring, the scheduled CI job, `hotel-demo up` | demo-pipeline task 1 (package, paths, features, CLI entry point) and task 3 (`score`) | the labels, monitoring and jobs files, the answer key | task 1 after demo-pipeline task 1 is on `main`; tasks 2 and 3 after demo-pipeline task 3 |

demo-pipeline task 1 and task 3 each get an early PR (their task issues say so), so the simulator can start.

## Interfaces

- `hotel_demo.paths`: `source_dir()`, `data_dir()`, `sim_dir()`, `mlflow_dir()`, each a `Path` from its environment variable or default, created on first use.
- `hotel_demo.features`: `LEAK_COLUMNS: frozenset[str]`, `FEATURES: tuple[str, ...]`, `TARGET = "is_canceled"`, `booking_date(df) -> pd.Series` (arrival date minus `lead_time`).
- `hotel_demo.pipeline.train(cutoff: date) -> str`: trains on bookings made before `cutoff` whose labels were known by `cutoff`, logs the run to MLflow, registers a new version of `hotel-cancellation`, moves the `production` alias to it, returns the version.
- `hotel_demo.pipeline.score(business_date: date) -> Path`: reads `bookings/<date>.parquet`, loads `models:/hotel-cancellation@production`, writes `predictions/<date>.parquet`, returns its path. Raises if the bookings file is missing or its schema is wrong.
- CLI `hotel-demo` (one entry point, subcommands added by each plan): `fetch`, `train --cutoff`, `score --date` (pipeline); `simulate`, `monitor --date`, `up` (simulator).
