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
- `train(cutoff)`: reads every `bookings/*` file with a date before `cutoff` and every `labels/*` file with a date up to `cutoff`, joins them on `booking_id` (bookings without a released label are left out), and takes `booking_changes` from the labels file (the final value). Fits LightGBM on a random 80% (fixed seed) and keeps the other 20% for `evaluate`; logs params, `FEATURES`, the training window, row counts and the model to MLflow at `tracking_uri()`; registers a new version. It never moves `production`.
- `evaluate(version)`: scores that version's held-out 20% and logs PR-AUC, ROC-AUC, precision and recall at the threshold (a parameter, default 0.5) and the Brier score to the version's run.
- `deploy(version)` moves `production` and records the previous target in the registered model's tags; `rollback()` moves `production` back to it and fails clearly when there is none.
- The fixture: a data folder of about 60 bookings over five consecutive days, with their labels, including a same-day cancellation.

**Tests (stop condition):** `train` on the fixture logs one run with the listed params and registers version 1 without setting `production`; it uses the labels' `booking_changes`; a label released after `cutoff` is not used; `evaluate` logs every listed metric; deploy, a second train and deploy, then `rollback` points `production` back at version 1; `rollback` with nothing to roll back fails with a clear message; the CLI subcommands exit 0 on success and non-zero with a message on failure. CI green on Ubuntu and Windows.

### Task 2: Scoring, the daily job, legacy monitoring

**Files:** `src/hotel_demo/{pipeline,monitoring,daily}.py`, the `score` and `daily` subcommands, tests.

**Notes:**
- `score(date)`: reads `bookings/<date>.parquet`, requires exactly the overview's columns, loads `models:/hotel-cancellation@production`, writes `predictions/<date>.parquet`. A missing file, a schema mismatch or no `production` model raises, naming the file and the missing or unexpected columns.
- `daily(date)`: runs `score`, then the monitoring, appending each run to `jobs/<date>.jsonl` with its status and error; a failed score is logged and the monitoring checks that do not need that day's predictions still run. Exit code non-zero if any step failed.
- Legacy monitoring; each check appends `{check, fired, value, threshold, detail}` to `monitoring/<date>.json`; the lines live in one constants block:
  - **Cancellation-rate alert:** share of `p_cancel >= 0.5` over the last 7 days of predictions; fires above 0.45.
  - **Drift report:** per numeric feature, a two-sample Kolmogorov-Smirnov test of the last 7 days of bookings against the production model's training window; fires on any p < 0.05.
  - **Accuracy alert:** accuracy over the labels released in the last 30 days, joined to their predictions; fires below 0.75.
- No scheduled retraining: a person runs `train`, `evaluate` and `deploy` by hand.

**Tests (stop condition):** `daily` on the fixture writes predictions, monitoring and jobs files; each check fires on a constructed input that crosses its line and stays quiet on one that does not; the monitoring JSON shape is exactly as above; a bookings file with a missing or extra column fails `score`, names the column, writes no predictions, and the other checks still run. CI green.

### Task 3: CI and the scheduled workflow

**Files:** `.github/workflows/scheduled.yml`, `tests/fixtures/`, README.

**Notes:**
- A daily scheduled workflow (and `workflow_dispatch` with no inputs) that downloads the sample data folder from the URL in the repository variable `SAMPLE_DATA_URL` (default: the fixture's archive on `main`), then runs `train`, `evaluate`, `deploy` and `daily` on it. A failing download fails the run at the "Fetch sample data" step, as an unavailable upstream source would.
- The README says how to set the variable.

**Tests (stop condition):** the workflow passes `actionlint` in CI; one manual run on the demo repo succeeds (its link in the PR).
