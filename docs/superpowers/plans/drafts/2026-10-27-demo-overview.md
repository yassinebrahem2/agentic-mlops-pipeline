# Demo Project — Plans Overview

> **Draft.** Missing: an advisor round with no blocking issues.

The demo shows every feature of the app on a hotel group's live ML system, and lets the evaluation measure the agents against the company's own monitoring. It has two parts:
- **The company**: the repository `yassinebrahem2/hotel-cancellation-demo`. Only what a real company would have: its pipeline, its daily job, its legacy monitoring, its CI. This is the project added to the app.
- **The world**: `bench/demo-world/` in this repository, a developer tool outside the product. It plays time: it delivers each day's bookings and the late outcomes into the company's data folder, runs the company's daily job, injects problems, and keeps the answer key and the list of planted flaws for the evaluation.

Facts and the story: `project_knowledge.md` (the demo entries of 2026-10-27).

**Story.** A hotel group (a city hotel and a resort) scores every new booking at booking time with the probability that it will be cancelled. The revenue team adds up the expected cancellations per arrival date and overbooks by that much. The goal is to fill rooms without turning away guests who hold a reservation. The company tracks its models in MLflow and runs simple monitoring of its own (the "legacy monitoring"), which is the app's baseline. Like a real mid-size company, its setup has flaws, planted on purpose for the agents to find.

**Where things live.**
- Plans, plan issues and task issues: this repository (`docs/superpowers/plans/`, the project board, milestone "Demo project"). Agents start from this repository, so its `AGENTS.md` applies in full.
- demo-company: code, branch, CI and PR in the demo repository; its owner works in a clone of it on the plan's branch. Its PR closes this repository's issues with the full reference, `Closes yassinebrahem2/agentic-mlops-pipeline#<n>`. No board automation follows demo PRs: the owner moves the cards (`AGENTS.md` rule 7). The lead's watcher follows both repositories, and the lead reviews and merges there the same way.
- demo-world: an ordinary plan of this repository.
- Both run CI on Ubuntu and Windows.

**Tech stack.** Python 3.12, uv, pandas, pyarrow, pytest, ruff, `argparse` for CLIs. The company adds LightGBM, scikit-learn (metrics), scipy (its drift test) and MLflow. The world adds `mlflow` only to start the server in `up`.

## Global constraints

- **Data.** Hotel Booking Demand (Antonio, Almeida and Nunes, *Data in Brief* 22, 2019, doi:10.1016/j.dib.2018.11.126), CC BY 4.0. Only the world reads the original CSV: `demo-world fetch` downloads it from a pinned URL and checks a pinned SHA-256. Neither repository commits it. Both READMEs credit the authors, link the licence and say the data is re-sliced.
- **Folders.** Outside both repositories, each overridable by an environment variable, defaults under `~/hotel-demo/`:
  - `HOTEL_DEMO_DATA` (`data/`): the company's production data, written by the world and the company's daily job; what an app data connection points at.
  - `HOTEL_DEMO_MLFLOW` (`mlflow/`): MLflow's SQLite store and artifacts (the company's).
  - `HOTEL_DEMO_WORLD` (`world/`): the world's own folder: the downloaded CSV, its state, the answer key, the planted-flaws list. Never inside the data folder.
- **One tracking URI.** In the company, `paths.tracking_uri()` is the only place MLflow's URI is chosen: `MLFLOW_TRACKING_URI` if set (under `up`, the server), else `sqlite:///<mlflow dir>/mlflow.db`. The port is `HOTEL_DEMO_MLFLOW_PORT`, default 5000.
- **Business dates.** Every file in the data folder carries its business date (the simulated day) in its name, `YYYY-MM-DD`. Nothing in the data depends on the wall clock.
- **Labels arrive late.** A booking's label is released on its cancellation date if cancelled, otherwise on its arrival date, and never before its booking date (a booking cancelled the day it was made gets its label that day).
- **The world and the company share no code.** The world calls the company's CLI as a subprocess; the data folder layout below and the CLI are their only interface.

## Data folder layout (the interface)

```
data/
  bookings/YYYY-MM-DD.parquet      booking_id, booking_date, arrival_date, then the booking columns   (world)
  labels/YYYY-MM-DD.parquet        booking_id, is_canceled, label_date, booking_changes               (world)
  predictions/YYYY-MM-DD.parquet   booking_id, business_date, model_version, p_cancel                 (company)
  monitoring/YYYY-MM-DD.json       the legacy monitoring's results for that day                       (company)
  jobs/YYYY-MM-DD.jsonl            one line per job run: job, business_date, started, ended, status, error (company)
```

**`bookings/*.parquet`, exactly these columns in this order** (parquet types; "null" = nullable, others never null; text columns are plain strings, never dictionary or categorical):

| Column | Type | Notes |
|---|---|---|
| `booking_id` | int64 | the source row number |
| `booking_date` | date32 | arrival date minus `lead_time` |
| `arrival_date` | date32 | |
| `hotel` | string | `City Hotel` or `Resort Hotel` |
| `lead_time` | int64 | |
| `arrival_month` | int8 | 1 to 12 |
| `arrival_week` | int8 | ISO week of `arrival_date` |
| `stays_in_weekend_nights` | int64 | |
| `stays_in_week_nights` | int64 | |
| `adults` | int64 | |
| `children` | int64, null | |
| `babies` | int64 | |
| `meal` | string, null | |
| `country` | string, null | |
| `market_segment` | string, null | |
| `distribution_channel` | string, null | |
| `is_repeated_guest` | int64 | 0 or 1 |
| `previous_cancellations` | int64 | |
| `previous_bookings_not_canceled` | int64 | |
| `reserved_room_type` | string | |
| `booking_changes` | int64 | always 0 here (none have happened yet) |
| `deposit_type` | string | |
| `agent` | int64, null | an ID |
| `company` | int64, null | an ID |
| `customer_type` | string | |
| `adr` | float64 | average daily rate |
| `total_of_special_requests` | int64 | |

**`labels/*.parquet`:** `booking_id` int64, `is_canceled` int64 (0 or 1), `label_date` date32, `booking_changes` int64 (the final value).

- The world converts the source's `"NULL"` (`agent`, `company`, `country`), `"NA"` (`children`) and `"Undefined"` (`meal`, `market_segment`, `distribution_channel`) markers to nulls.
- Every file is written to a temporary name in the same folder and renamed.
- **Contract test.** The demo-world CI workflow checks out the demo repository's `main`, generates five days with the world, and runs the company's real `daily` on them; a schema disagreement fails it.

## The company's CLI (`hotel-demo`)

`train --cutoff`, `evaluate --version`, `deploy --version`, `rollback`, `score --date`, `daily --date` (score, then every monitoring check). Onboarding finds these as the company's commands; the stage 5 agents use them. Exit code 0 on success, non-zero with a message on failure. The world always runs them, and MLflow's server, from the company's own environment (`uv run --project <company checkout> ...`), so there is one MLflow version.

**The production schedule is a file.** `ops/schedule.toml` in the company repository lists the company's scheduled jobs (name, command, every N business days); the world runs exactly what it lists, as the company's ops host would. Onboarding records it as the team's scheduler. The scheduled GitHub workflow is only a CI smoke run on sample data.

**Checks are files.** `daily` runs every check module in `src/hotel_demo/checks/`, so an agent's PR that adds a check file takes effect on the next run once merged. The legacy checks write their own result shape; the app reads them through a parser (stage 4). Who merges agents' PRs: the user in a live demo; the world in evaluation runs, after a fixed delay in simulated days recorded with the run.

## Plans

| Plan | Repository, branch | Covers | Consumes | Starts |
|---|---|---|---|---|
| demo-company | demo repo, `plan/demo-company` | Package, train, evaluate, deploy, rollback, score, the daily job, legacy monitoring, CI and the scheduled workflow | the layout above | now |
| demo-world | this repo, `plan/demo-world` | `bench/demo-world/`: fetch, history backfill, the day loop, injected problems, the answer key, the planted-flaws list, the PR merger for evaluation runs, `up` | the layout above; task 3 also the company's CLI on the demo repo's `main` | tasks 1 and 2 now; task 3 after demo-company merges |

The planted flaws and the injected scenarios are listed in the demo-world plan (the evaluation's answer key). The demo-company plan describes the company's setup as plain choices, not as flaws.
