# Demo — World Implementation Plan

> **Draft.** Missing: an advisor round with no blocking issues.

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. Read `2026-10-27-demo-overview.md` first: the two parts, folders, data layout, the company's CLI.

**Goal:** The world around the demo company, as a developer tool in this repository (`bench/demo-world/`, outside the product): it downloads the source data, gives the company its history, plays time day by day (new bookings, late outcomes, the company's daily job), injects problems at known dates, and keeps the answer key and the planted-flaws list the evaluation scores the agents against.

**Branch:** `plan/demo-world`. **Consumes:** the overview's data layout; task 3 also the company's CLI (demo-company merged). **Produces:** the bookings and labels files, `world/answer_key.jsonl`, `world/planted_flaws.json`, the `demo-world` CLI.

## Planted flaws (the company's setup, by design)

What a real mid-size company plausibly gets wrong. The agents' first audit should find them. `demo-world` writes this list to `world/planted_flaws.json` (id, category, where, what):
1. **Training uses a value known only after the stay.** `train` takes `booking_changes` from the labels (its final value), while bookings are scored with 0. Production precision is lower than `evaluate` claims.
2. **Random validation split.** `evaluate` scores a random 20% of the training window, which overstates skill against later bookings.
3. **Accuracy-only performance alert.** Accuracy looks fine because most bookings are not cancelled.
4. **No calibration check.** Nothing checks whether a predicted 30% means 30% cancel, which overbooking depends on.
5. **Retraining by hand only.** Nothing schedules it, so decay builds up.

## Tasks

| Task | Title | Starts |
|---|---|---|
| 1 | Package, fetch, history backfill | now |
| 2 | The day loop, injected problems, answer key | now |
| 3 | The company's daily job in the loop, `up`, the CI failure switch | after demo-company merges |

---

### Task 1: Package, fetch, history backfill

**Files:** `bench/demo-world/pyproject.toml` (its own uv project, console script `demo-world`), `uv.lock`, `bench/demo-world/src/demo_world/{__init__,paths,source,fetch,backfill,cli}.py`, `bench/demo-world/tests/` (with a small source CSV fixture), `bench/demo-world/README.md` (what it is, data credit and licence), `.github/workflows/demo-world.yml` (ruff, format check, pytest; Ubuntu and Windows; runs only on changes under `bench/demo-world/`).

**Notes:**
- `paths`: `data_dir()` (`HOTEL_DEMO_DATA`), `world_dir()` (`HOTEL_DEMO_WORLD`), defaults under `~/hotel-demo/`.
- `fetch`: downloads the TidyTuesday 2020-02-11 `hotels.csv` (a copy of the paper's data) from one pinned URL, checks one pinned SHA-256, writes `world_dir()/hotels.csv`; a wrong hash deletes the partial file and fails naming both hashes.
- `source`: loads the CSV, converts the missing markers to nulls, adds `booking_id`, `booking_date`, `arrival_date`, `arrival_month`, `arrival_week` and the label release date (overview rules), and builds a day's bookings and labels frames with exactly the overview's columns (`booking_changes` 0 in bookings, final in labels).
- `backfill --until 2016-01-01`: writes the bookings files for every booking date from 2015-07-01 to the day before, and the labels files for every release date up to the day before. Refuses to run on a data folder that already has files.

**Tests (stop condition):** fetch with a locally served fixture and its hash writes it, a wrong hash fails and leaves nothing; the bookings and labels frames have exactly the overview's columns, `booking_changes` is 0 in bookings and final in labels; release dates follow the overview (same-day cancellation included); backfill on the fixture writes the expected files and refuses a non-empty folder. CI green on Ubuntu and Windows.

### Task 2: The day loop, injected problems, answer key

**Files:** `bench/demo-world/src/demo_world/{simulate,scenarios}.py`, `bench/demo-world/scenarios/default.toml`, tests.

**Notes:**
- `simulate --start 2016-01-01 --end 2017-03-31 --seconds-per-day 120`: state in `world_dir()/state.json` (the last business date done); resumes after it. `--seconds-per-day 0` runs as fast as possible. Each day: write that day's bookings file, then that day's labels file (every written booking whose label is released that day). The step that runs the company's daily job is added in task 3.
- Scenarios: a TOML file of injected problems, each with a start and end date: `null_spike` (a column set to null for a share of rows), `schema_break` (a column renamed or dropped), `out_of_range` (`adr` multiplied), and `score_failure` and `ci_failure` (acted on in task 3). `default.toml` holds one of each at dates spread over 2016, away from the natural shift of late 2016, and lists the natural shifts to score (the late-2016 non-refundable group bookings; the rise in online travel agent share).
- Every injection and listed natural shift is appended to `world_dir()/answer_key.jsonl` as `{kind, category, column, start, end}`; `world_dir()/planted_flaws.json` holds the list above. Neither is ever written into the data folder.

**Tests (stop condition):** five fast days on the fixture write the expected files; every written booking's label is released exactly once, never before its booking date; a restart resumes without rewriting files; each injected kind changes exactly the files and dates it should; the answer key lists every injection and natural shift; nothing in the data folder mentions the answer key or the planted flaws.

### Task 3: The company's daily job in the loop, `up`, the CI failure switch

**Files:** `simulate.py`, `bench/demo-world/src/demo_world/{company,up}.py`, tests, README.

**Notes:**
- `company.py`: the one place the world runs the company's CLI, as a subprocess in a given checkout of the demo repository (`HOTEL_DEMO_COMPANY`, a path), with `HOTEL_DEMO_DATA` and `MLFLOW_TRACKING_URI` passed through. It returns the exit code and output; a failure is logged and never stops the loop.
- Before the first day, if the company has no `production` model: `train --cutoff <start>`, `evaluate`, `deploy`. Each day after the files: `daily --date <day>`. During a `score_failure` window the world withholds the day's bookings file until after `daily` has run, so the company's scoring fails on a missing file, as with a late upstream delivery.
- `ci_failure`: at the window's start the world sets the demo repo's `SAMPLE_DATA_URL` variable to a missing file (`gh api`), and restores it at the end; both changes go in the answer key.
- `up`: starts `mlflow server` on the company's store at `HOTEL_DEMO_MLFLOW_PORT`, sets `MLFLOW_TRACKING_URI`, then runs `simulate`; Ctrl+C stops both cleanly.

**Tests (stop condition):** with a fake company CLI (a script recording its calls), the loop bootstraps once and calls `daily` once per day with the right environment; a failing `daily` is logged and the next day runs; a `score_failure` window makes the real company's `daily` fail on that day (one test against the demo repo's CLI, marked to run only when `HOTEL_DEMO_COMPANY` is set); `ci_failure` calls the variable API at start and end (mocked `gh`); `up` starts and stops both processes (a short run).
