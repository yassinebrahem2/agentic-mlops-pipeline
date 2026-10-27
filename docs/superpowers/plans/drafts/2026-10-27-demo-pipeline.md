# Demo — Pipeline Implementation Plan

> **Draft.** Missing: an advisor round with no blocking issues.

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says, in the demo repository (`yassinebrahem2/hotel-cancellation-demo`). Read `2026-10-27-demo-overview.md` first: story, folders, leak list, data layout and interfaces.

**Goal:** The hotel group's ML pipeline: download the data, train and evaluate a cancellation model logged to MLflow, register it as the production model, and score each day's new bookings with it.

**Branch:** `plan/demo-pipeline` (demo repo). **Consumes:** nothing. **Produces:** `hotel_demo.paths`, `hotel_demo.features`, `train`, `score`, the `hotel-demo` CLI with `fetch`, `train` and `score`.

## Tasks

| Task | Title | PR |
|---|---|---|
| 1 | Package, paths, features, fetch | early PR (the simulator needs it) |
| 2 | Train, evaluate, register | |
| 3 | Batch scoring | early PR (the simulator needs it) |

---

### Task 1: Package, paths, features, fetch

**Files:** `pyproject.toml` (uv, Python 3.12, console script `hotel-demo`), `uv.lock`, `src/hotel_demo/{__init__,paths,features,fetch,cli}.py`, `tests/`, `.github/workflows/ci.yml` (ruff, ruff format --check, pytest on ubuntu), `README.md` (story, how to run, data credit and licence).

**Notes:**
- `fetch` downloads the CSV from one pinned URL (the TidyTuesday 2020-02-11 `hotels.csv`, a copy of the paper's data; put the URL and its SHA-256 in one constant), verifies the hash, and writes `source_dir()/hotels.csv`. A wrong hash deletes the partial file and fails with the expected and actual hash.
- `features.FEATURES` is every source column except `LEAK_COLUMNS` and the date parts used only to build dates (`arrival_date_year`, `arrival_date_month`, `arrival_date_day_of_month`, `arrival_date_week_number`). Categoricals are typed as pandas `category`. The literal `"NULL"` in `agent`, `company` and `country`, and `"NA"` in `children`, are read as missing.
- `cli.py` is a `typer` or `argparse` app with one subcommand registry, so the simulator plan adds its subcommands in its own module without editing the pipeline's.
- Tests use a small CSV fixture of real-shaped rows (about 50) checked into `tests/fixtures/`, never the full data.

**Tests (stop condition):** paths honour their environment variables and default under `~/hotel-demo/`; `fetch` with a served fixture file and its hash writes it, and with a wrong hash fails and leaves no file; `FEATURES` and `LEAK_COLUMNS` do not overlap and together with the date parts cover every source column; `booking_date` on the fixture equals arrival minus lead time; the missing-value markers load as missing. CI green.

### Task 2: Train, evaluate, register

**Files:** `src/hotel_demo/pipeline.py` (`train`), `src/hotel_demo/evaluate.py`, CLI subcommand `train`, tests.

**Notes:**
- `train(cutoff)`: bookings made before `cutoff` whose label was known by `cutoff` (the release rule in the overview); LightGBM with fixed seed; a time-ordered validation split (the last 20% of the window by booking date), never a random split.
- Evaluate on the validation split: PR-AUC, ROC-AUC, precision and recall at the operating threshold (the threshold is a parameter, default 0.5), Brier score and a 10-bin calibration table. Log params, metrics, the calibration table (as an artifact) and the model to MLflow, with the training window and row counts as tags.
- Register a new version of `hotel-cancellation` and move the alias `production` to it. MLflow runs against `sqlite:///<mlflow_dir>/mlflow.db` with artifacts under `mlflow_dir()/artifacts`, in-process for `train` (no server needed); the same store is what `hotel-demo up` serves.

**Tests (stop condition):** on the fixture, `train` logs one run with every metric above, registers version 1 and points `production` at it; a second `train` creates version 2 and moves the alias; no `LEAK_COLUMNS` column reaches the model (the logged feature list equals `FEATURES`); a label released after `cutoff` is not in the training set. CI green.

### Task 3: Batch scoring

**Files:** `src/hotel_demo/pipeline.py` (`score`), CLI subcommand `score`, tests.

**Notes:**
- `score(date)`: reads `data_dir()/bookings/<date>.parquet`, checks its columns against `FEATURES` (+ `booking_id`), loads `models:/hotel-cancellation@production`, writes `predictions/<date>.parquet` with `booking_id`, `business_date`, `model_version`, `p_cancel`. Written to a temporary name and renamed, so a reader never sees half a file.
- A missing file or a schema mismatch raises with a message naming the file and the missing or unexpected columns.

**Tests (stop condition):** scoring the fixture's bookings writes one prediction per booking with the production version; after a retrain the next score uses the new version; a bookings file with a missing column fails with that column named and writes nothing. CI green.
