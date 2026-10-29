# Demo — World Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. Read `docs/superpowers/plans/2026-10-27-demo-overview.md` first: the two parts, folders, data layout, the company's CLI.

**Goal:** The world around the demo company, as a developer tool in this repository (`bench/demo-world/`, outside the product): it downloads the source data, gives the company its history, plays time day by day (new bookings, late outcomes, the company's daily job), injects problems at known dates, and keeps the answer key and the planted-flaws list the evaluation scores the agents against.

**Branch:** `plan/demo-world`. **Consumes:** the overview's data layout; task 3 also the company's CLI (demo-company merged). **Produces:** the bookings and labels files, `world/answer_key.jsonl`, `world/planted_flaws.json`, the `demo-world` CLI.

## Planted flaws (the company's setup, by design)

What a real mid-size company plausibly gets wrong. The agents' first audit should find them. `demo-world` writes this list to `world/planted_flaws.json` (id, category, where, what). The numbers come from a replay of the data close to this setup (first model on arrivals before 2016-01-01, never retrained); task 3 re-measures them on the built system.
1. **Random validation split.** `evaluate` scores a random 20% of the training window: PR-AUC about 0.98, against about 0.66 on later bookings.
2. **Accuracy-only performance alert.** Accuracy stays between 0.66 and 0.78 while PR-AUC decays (about 0.74 in January 2016 to 0.45 in October), because about 65% of bookings are never cancelled.
3. **No calibration check.** The model predicts a mean cancellation chance of about 0.15 against a real rate of about 0.35, which is what overbooking depends on.
4. **Retraining by hand only.** Nothing schedules it, so the decay builds up.
5. **Drift check blind to categories.** The drift report looks at four numeric columns only, so the late-2016 shift (non-refundable group bookings, in `deposit_type` and `market_segment`) goes unseen.
6. **Training uses a value known only after the stay (minor).** `train` takes `booking_changes` from the labels (its final value), while bookings are scored with 0. On this data it costs little (PR-AUC 0.661 against 0.665): a finding, not a failure.
7. **MLflow without authentication.** Anyone who can reach the tracking server can change the production model; the agents should report it and propose enabling MLflow's authentication. The world starts the server, so a PR to the company cannot close it: this flaw is scored on the audit report only.

## Tasks

| Task | Title | Starts |
|---|---|---|
| 1 | Package, fetch, history backfill | now |
| 2 | The day loop, injected problems, answer key | now |
| 3 | The company's daily job in the loop, the PR merger, `up`, CI runs | after demo-company merges |

---

### Task 1: Package, fetch, history backfill

**Files:** `AGENTS.md` (one line under the commands: how to run `bench/demo-world/`), `bench/demo-world/pyproject.toml` (its own uv project, console script `demo-world`), `uv.lock`, `bench/demo-world/src/demo_world/{__init__,paths,source,fetch,backfill,cli}.py`, `bench/demo-world/tests/` (with a small source CSV fixture shaped like the company's: a history slice with both classes that arrived before the fixture's start date, then five booking days), `bench/demo-world/README.md` (what it is, data credit and licence), `.github/workflows/demo-world.yml` (ruff, format check, pytest; Ubuntu and Windows; runs on changes under `bench/demo-world/`).

**Notes:**
- `paths`: `source_dir()` (`HOTEL_DEMO_SOURCE`), `data_dir()` (`HOTEL_DEMO_DATA`), `world_dir()` (`HOTEL_DEMO_WORLD`), defaults under `~/hotel-demo/`.
- `fetch`: caches the source once for every run, in `~/hotel-demo/source/` (`HOTEL_DEMO_SOURCE`); downloads the TidyTuesday 2020-02-11 `hotels.csv` (a copy of the paper's data) from one pinned URL, checks one pinned SHA-256, writes `hotels.csv` there; a wrong hash deletes the partial file and fails naming both hashes.
- `source`: loads the CSV, converts the missing markers to nulls, adds `booking_id`, `booking_date`, `arrival_date`, `arrival_month`, `arrival_week` and the label release date (overview rules), and builds a day's bookings and labels frames with exactly the overview's columns (`booking_changes` 0 in bookings, final in labels).
- `backfill --until 2016-07-01`: writes the bookings files for every booking date before `--until` (no lower bound: long-lead bookings made before July 2015 are history too), and the labels files for every release date before `--until`. Refuses to run on a data folder that already has files.

**Tests (stop condition):** fetch with a locally served fixture and its hash writes it, a wrong hash fails and leaves nothing; the bookings and labels frames have exactly the overview's columns, `booking_changes` is 0 in bookings and final in labels; release dates follow the overview (same-day cancellation included); backfill on the fixture writes the expected files and refuses a non-empty folder. CI green on Ubuntu and Windows.

### Task 2: The day loop, injected problems, answer key

**Files:** `bench/demo-world/src/demo_world/{simulate,scenarios}.py`, `bench/demo-world/scenarios/default.toml`, tests.

**Notes:**
- `simulate --start 2016-07-01 --end 2017-03-31 --seconds-per-day 120`: state in `world_dir()/state.json` (the last business date done); resumes after it. `--seconds-per-day 0` runs as fast as possible. Each day: write that day's bookings file, then that day's labels file (every written booking whose label is released that day). The step that runs the company's daily job is added in task 3.
- Scenarios: a TOML file of injected problems, each with a start and end date: `null_spike` (a column set to null for a share of rows; `default.toml` targets `country` and `adr`), `schema_break` (a column renamed or dropped), `out_of_range` (`adr` multiplied), and `score_failure` and `ci_failure` (acted on in task 3). `default.toml` holds one of each at dates spread over 2016, away from the natural shift of late 2016, and lists the natural shift to score: the non-refundable group bookings of November and December 2016 (only shifts with a clear start are scored; the gradual rise in online travel agent share is not).
- Every injection and listed natural shift is appended to `world_dir()/answer_key.jsonl` as `{kind, category, column, start, end}`; `world_dir()/planted_flaws.json` holds the list above. Neither is ever written into the data folder.

**Tests (stop condition):** five fast days on the fixture write the expected files; every written booking's label is released exactly once, never before its booking date; a restart resumes without rewriting files; each injected kind changes exactly the files and dates it should; the answer key lists every injection and natural shift; nothing in the data folder mentions the answer key or the planted flaws.

### Task 3: The company's daily job in the loop, the PR merger, `up`, CI runs

**Files:** `simulate.py`, `bench/demo-world/src/demo_world/{company,up}.py`, `.github/workflows/demo-world.yml` (adds the overview's contract test, and a daily schedule trigger next to the path trigger), tests, README.

**Notes:**
- `company.py`: the one place the world runs the company's CLI, as `uv run --project <checkout> hotel-demo ...` in a checkout of the demo repository (`HOTEL_DEMO_COMPANY`, a path), with `HOTEL_DEMO_DATA` and `MLFLOW_TRACKING_URI` passed through. It returns the exit code and output; a failure is logged and never stops the loop. Before each day it updates the checkout to the latest `main` (`git pull --ff-only`), so merged PRs take effect.
- **The company repository is a setting, `HOTEL_DEMO_REPO`.** No run, evaluation or live demo, touches `yassinebrahem2/hotel-cancellation-demo` itself, so every run starts from the same company and can be rehearsed: `demo-world new-run <name>` creates a public copy from it (the demo repository is a template; public, so its Actions runs cost no minutes) with `gh api`, clones it under `~/hotel-demo/runs/<name>/company`, creates fresh `data`, `mlflow` and `world` folders beside it, and writes every setting (`HOTEL_DEMO_REPO`, `HOTEL_DEMO_COMPANY`, `HOTEL_DEMO_DATA`, `HOTEL_DEMO_MLFLOW`, `HOTEL_DEMO_WORLD`, `HOTEL_DEMO_MLFLOW_PORT`) to `~/hotel-demo/runs/<name>/run.env`, which every other command loads with `--run <name>`; it picks a free port for MLflow. Anything that touches GitHub requires `HOTEL_DEMO_REPO`. The README says how to delete old run copies (`gh repo delete`) and their folders. So every run, and every rehearsal, starts from the same company with no data and no model; the world refuses to run when `HOTEL_DEMO_REPO` names the original (the contract test checks out the original's `main` without setting it, and is not refused). A live demo uses its own copy too, without `--merge-after-days` (the user merges by hand).
- **PR merger (evaluation runs only, `--merge-after-days N`):** an open PR on `HOTEL_DEMO_REPO` whose head branch is an agent branch (`agent/<agent>/<task>`, the stage 2 branch name) is merged by the world once N simulated days have passed since it opened and its checks are green (`gh pr merge --merge`). A PR with a conflict or red checks is recorded as not merged and left for the agents. Each merge or refusal, with its delay, goes in the answer key. Without the option the world never merges (a live demo, where the user merges).
- Before the first day, if the company has no `production` model: `train --cutoff <start>`, `evaluate`, `deploy`. Each day after the files: every job in the company checkout's `ops/schedule.toml` that is due that day (`every` business days), in file order, with `{date}` in its command replaced by the day (the world knows nothing else about the company's CLI). An agent's PR that adds a job there (a weekly retrain, a new check) runs once merged. During a `score_failure` window the world withholds the day's bookings file until after `daily` has run, so the company's scoring fails on a missing file, as with a late upstream delivery.
- **CI runs at simulation speed:** every `--ci-every N` simulated days (default 7) the world starts `HOTEL_DEMO_REPO`'s scheduled workflow with `workflow_dispatch` (`gh api`), so the Actions history moves with simulated time. Each dispatch is recorded in the world's folder (business date, real time, run ID), so CI incidents can be scored in business days even at full speed. Each dispatch is recorded in the world's folder (business date, real time, run ID), so CI incidents can be scored in business days even at full speed.
- `ci_failure`: at the window's start the world sets `HOTEL_DEMO_REPO`'s `SAMPLE_DATA_URL` variable to a missing file and restores it at the end; the answer key records the window in business dates and in real start and end times (the CI adapter sees real times).
- README: how to fast-forward a live demo (`--seconds-per-day 0` up to a chosen date, then a slower pace; the full span at 120 seconds a day takes about 15 hours).
- `up`: starts `mlflow server` from the company's environment on its store at `HOTEL_DEMO_MLFLOW_PORT`, sets `MLFLOW_TRACKING_URI`, then runs `simulate`; Ctrl+C stops both cleanly, on Windows too (process groups).

**Review fix (#387 item 4, 2026-10-29):** in hotel-cancellation-demo, `tests/test_daily.py` writes `bad_syntax_check.py` into `src/`, so an interrupted run leaves it there and breaks the next daily run. Write it into a temporary checks directory and point discovery at it. The other #387 items are recorded in `docs/known-debt.md`.

**Tests (stop condition):** with a fake company CLI (a script recording its calls), the loop bootstraps once and runs each scheduled job on its due days with the right environment, and a job added to `ops/schedule.toml` between days runs from the next due day; `new-run` calls the template API and writes `run.env` (mocked `gh`); two `new-run`s get disjoint folders, and the second bootstraps its own model; a failing `daily` is logged and the next day runs; a `score_failure` window makes the real company's `daily` fail on that day (one test against the demo repo's CLI, marked to run only when `HOTEL_DEMO_COMPANY` is set); `ci_failure` and the CI runs call the right APIs at the right days (mocked `gh`); the merger merges a PR after N simulated days and never without the option (mocked `gh`); the world refuses to run against the original demo repository; the checkout is updated before each day; `up` starts and stops both processes (a short run, in the Ubuntu and Windows jobs); the contract test passes in CI.
- **A full-data run** from 2016-07-01 to 2017-03-31 at full speed (the start moved from 2016-01-01 by Decision #423: January 2016's drift check fired on 4 days, so the plan's fallback applies), with no injections; the PR pastes the first model's PR-AUC and mean predicted versus actual cancellation rate per month, and each legacy check's firing days per month. The first simulated month, July 2016, must be quiet: at most 2 firing days per check. If it is not, stop and raise it in the plan issue (thresholds change only through a plan edit). Every `scenarios/default.toml` window starts on or after 2016-08-01 and keeps its length and order; the answer key follows.
