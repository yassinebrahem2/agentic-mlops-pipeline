# Demo — Company Implementation Plan

> **Draft.** Missing: an advisor round with no blocking issues.

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says, in the demo repository (`yassinebrahem2/hotel-cancellation-demo`), on `plan/demo-company`. Read `2026-10-27-demo-overview.md` first: story, folders, data layout, CLI. Build the company's setup exactly as written here, including choices you would make differently: they are deliberate (the demo-world plan says why). Do not comment on them in code, the README or commit messages.

**Goal:** The hotel group's ML system: train and evaluate a cancellation model from its own data, logged to MLflow; deploy and roll back through the `production` alias; score each day's bookings; run its legacy monitoring; and a CI history.

**Branch:** `plan/demo-company` (demo repo). **Consumes:** the overview's data layout. **Produces:** the company's CLI and the predictions, monitoring and jobs files.

## Tasks

| Task | Title |
|---|---|
| 1 | Package, train, evaluate, deploy, rollback |
| 2 | Scoring, the daily job, legacy monitoring |
| 3 | CI and the scheduled workflow |

---

### Task 1: Package, train, evaluate, deploy, rollback

**Files:** `pyproject.toml` (uv, Python 3.12, console script `hotel-demo`), `uv.lock`, `src/hotel_demo/{__init__,paths,features,pipeline,cli}.py`, `tests/`, `tests/fixtures/` (a small data folder), `.github/workflows/ci.yml` (ruff check, ruff format --check, pytest; Ubuntu and Windows), `README.md` (the company, how to run, data credit and licence).

**Notes:**
- `paths`: `data_dir()`, `mlflow_dir()`, `tracking_uri()` as the overview says.
- `features`: `TARGET = "is_canceled"`; `FEATURES`: every booking column (overview) except `booking_id`, `booking_date`, `arrival_date`; the model name `hotel-cancellation` and alias `production`. One definition each.
- `train(cutoff)`: reads the `bookings/*` and `labels/*` files, joins them on `booking_id`, and keeps the bookings whose `arrival_date` is before `cutoff` (every such booking's label is known by then; selecting on "label known" instead would keep early cancellations of later stays and leak the label). Takes `booking_changes` from the labels file (the final value). Fits LightGBM on a random 80% (fixed seed) and keeps the other 20% for `evaluate`; logs params, `FEATURES`, the training window, row counts and the model to MLflow at `tracking_uri()`; registers a new version. It never moves `production`.
- `evaluate(version)`: scores that version's held-out 20% and logs PR-AUC, ROC-AUC, precision and recall at the threshold (a parameter, default 0.5) and the Brier score to the version's run.
- `deploy(version)` moves `production` and records the previous target in the registered model's tags; `rollback()` moves `production` back to it and fails clearly when there is none.
- The fixture: a data folder with a history slice (about 200 bookings that arrived before the fixture's cutoff, both classes, with their labels) and five consecutive scored days after it (about 60 bookings, including a same-day cancellation). LightGBM's minimum leaf size is a `train` parameter, so tests on this small data pass without changing the production default.
- `train` logs the held-out 20% `booking_id`s as an artifact of the run, and `evaluate(version)` reads them, so evaluating a version always scores the same rows.

**Tests (stop condition):** `train` on the fixture logs one run with the listed params and registers version 1 without setting `production`; it uses the labels' `booking_changes`; a label released after `cutoff` is not used; `evaluate` logs every listed metric; deploy, a second train and deploy, then `rollback` points `production` back at version 1; `rollback` with nothing to roll back fails with a clear message; the CLI subcommands exit 0 on success and non-zero with a message on failure. CI green on Ubuntu and Windows.

### Task 2: Scoring, the daily job, legacy monitoring

**Files:** `src/hotel_demo/{pipeline,daily}.py`, `src/hotel_demo/checks/{__init__,cancellation_rate,drift,accuracy}.py`, `ops/schedule.toml`, the `score` and `daily` subcommands, tests.

**Notes:**
- `score(date)`: reads `bookings/<date>.parquet`, requires exactly the overview's columns, loads `models:/hotel-cancellation@production`, writes `predictions/<date>.parquet`. A missing file, a schema mismatch or no `production` model raises, naming the file and the missing or unexpected columns.
- `daily(date)`: runs `score`, then every check module in `checks/` (each exposes `run(date)` and writes its own results; `daily` does not assume their shape), appending each run to `jobs/<date>.jsonl` with its status and error; a failed score is logged and the checks that do not need that day's predictions still run. Exit code non-zero if any step failed.
- The three existing checks; each appends `{check, fired, value, threshold, detail}` to `monitoring/<date>.json`; each check's line lives in its own module as one constant:
  - **Cancellation-rate alert** (`cancellation_rate.py`): share of `p_cancel >= 0.5` over the last 7 days of predictions; fires above 0.30.
  - **Drift report** (`drift.py`): for `adults`, `stays_in_week_nights`, `stays_in_weekend_nights` and `children`, the two-sample Kolmogorov-Smirnov statistic of the last 7 days of bookings against the production model's training window; fires when any statistic is above 0.15.
  - **Accuracy alert** (`accuracy.py`): accuracy over the labels released in the last 30 days, joined to their predictions; fires below 0.60.
- `ops/schedule.toml`: the company's production schedule on its ops host, one entry per job: `name`, `command` (a `hotel-demo` command line, with `{date}` where the business date goes), `every` (business days). It lists only `daily --date {date}`, every day. No scheduled retraining: a person runs `train`, `evaluate` and `deploy` by hand.

**Tests (stop condition):** `ops/schedule.toml` parses and every command in it is a valid `hotel-demo` command; `daily` on the fixture writes predictions, monitoring and jobs files; a check module added to `checks/` in a test runs on the next `daily`; each check fires on a constructed input that crosses its line and stays quiet on one that does not; the monitoring JSON shape is exactly as above; a bookings file with a missing or extra column fails `score`, names the column, writes no predictions, and the other checks still run. CI green.

### Task 3: CI and the scheduled workflow

**Files:** `.github/workflows/scheduled.yml`, `tests/fixtures/`, README.

**Notes:**
- A daily scheduled workflow (and `workflow_dispatch` with no inputs, which the world uses to run it at simulation speed) that downloads the sample data folder from the URL in the repository variable `SAMPLE_DATA_URL` (default: the fixture's archive on `main`), then runs `train`, `evaluate`, `deploy` and `daily` on it. A failing download fails the run at the "Fetch sample data" step, as an unavailable upstream source would.
- The README says how to set the variable, and that this workflow is the CI smoke run, while `ops/schedule.toml` is the production schedule.

**Tests (stop condition):** the workflow passes `actionlint` in CI; one manual run on the demo repo succeeds (its link in the PR).
