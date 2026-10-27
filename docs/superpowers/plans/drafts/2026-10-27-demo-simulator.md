# Demo — Simulator Implementation Plan

> **Draft.** Missing: an advisor round with no blocking issues.

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says, in the demo repository (`yassinebrahem2/hotel-cancellation-demo`). Read `2026-10-27-demo-overview.md` first: story, folders, data layout and interfaces.

**Goal:** Make the demo a live system: time passes day by day, new bookings arrive and are scored, labels arrive later, the company's legacy monitoring runs, problems are injected at known dates, and an answer key records them.

**Branch:** `plan/demo-simulator` (demo repo). **Consumes:** demo-pipeline task 1 (package, `paths`, `features`, CLI) and task 3 (`score`). **Produces:** the labels, monitoring and jobs files; the answer key; `hotel-demo simulate`, `monitor` and `up`.

## Tasks

| Task | Title | Starts |
|---|---|---|
| 1 | The day loop | after demo-pipeline task 1 |
| 2 | Legacy monitoring and the retrain job | after demo-pipeline task 3 |
| 3 | Injected problems, answer key, scheduled CI job, `up` | after demo-pipeline task 3 |

---

### Task 1: The day loop

**Files:** `src/hotel_demo/simulate.py`, its CLI subcommand, tests.

**Notes:**
- State in `sim_dir()/state.json`: the current business date. `simulate --start 2016-01-01 --end 2017-03-31 --seconds-per-day 120` resumes from the state if present. `--seconds-per-day 0` runs as fast as possible (tests, eval).
- Each business day, in order, each step logged to `jobs/<date>.jsonl` with status and error:
  1. write `bookings/<date>.parquet`: source rows whose booking date is that day, with `booking_id`, minus the label fields;
  2. run `score(date)`;
  3. write `labels/<date>.parquet`: every earlier booking whose label is released that day (overview rule);
  4. run the daily monitoring (task 2), once it exists.
- A failing step is logged and the day goes on with the next step where it can (no predictions means no monitoring of predictions that day); the loop never stops on a job failure, as a real scheduler would not.
- Before the first day: if no `production` model exists, `train(cutoff=start)`.

**Tests (stop condition):** five fast days on the fixture write the expected bookings, predictions and labels files; every booking is released exactly once, on the right day; a restart resumes at the next day; a failing `score` is logged in `jobs` and the next day still runs.

### Task 2: Legacy monitoring and the retrain job

**Files:** `src/hotel_demo/monitoring.py`, `monitor` CLI subcommand, the retrain job in `simulate.py`, tests.

**Notes:** deliberately simple, what a mid-size hotel group plausibly runs; each check writes its result into `monitoring/<date>.json` as `{check, fired: bool, value, threshold, detail}`:
- **Cancellation-rate alert:** the share of predicted cancellations (`p_cancel >= threshold`) over the last 7 days of predictions; fires above a fixed line (0.45).
- **Drift report:** per numeric feature, a two-sample Kolmogorov-Smirnov test of the last 7 days of bookings against the training window; fires on any p < 0.05.
- **Label-based accuracy alert:** accuracy over labels released in the last 30 days; fires below a fixed line (0.75).
- **Monthly retrain:** on the first business day of each month, `train(cutoff=date)`, logged as a job.
The lines live in one constants block in `monitoring.py`.

**Tests (stop condition):** each check fires on a constructed input that crosses its line and stays quiet on one that does not; the JSON shape is exactly as above; the retrain runs on the first of a month only and moves `production`.

### Task 3: Injected problems, answer key, scheduled CI job, `up`

**Files:** `src/hotel_demo/scenarios.py`, `scenarios/default.toml`, `simulate.py`, `src/hotel_demo/up.py`, `.github/workflows/scheduled.yml`, tests, README.

**Notes:**
- A scenario file lists injected problems with a start date and an end date: `null_spike` (a column set to missing for a share of rows), `schema_break` (a column renamed or dropped), `out_of_range` (prices multiplied), `score_failure` (the scoring step raises). `default.toml` holds one of each at dates spread over 2016, away from the natural late-2016 shift.
- Every injection, and every natural shift the lead lists in the scenario file for scoring, is appended to `sim_dir()/answer_key.jsonl`: `{kind, category, column, start, end}`. The answer key is never written into the data folder.
- `up`: starts `mlflow server` on the demo's store (port 5000), then `simulate`; Ctrl+C stops both cleanly.
- `scheduled.yml`: a daily scheduled workflow (and `workflow_dispatch` with an input `fail: bool`) that runs train and score on the fixture, so the repository has a real Actions history; `fail=true` makes the run fail at a named step, for the pipeline-failure scenario.

**Tests (stop condition):** each injected kind changes exactly the files and dates it should; the answer key lists every injection with its dates; nothing under the data folder mentions the answer key; `up` starts and stops both processes (test with a short run); the workflow passes `actionlint` (or a YAML check) in CI.
