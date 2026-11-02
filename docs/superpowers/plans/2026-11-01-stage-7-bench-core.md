# Stage 7: Bench Core Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. Five tasks, one PR for the branch, plus two early PRs (`AGENTS.md` rule 5): after task 3 (the agents plan consumes the scorer and the report) and after task 4 (the datasets plan consumes the dataset interface and the replay). Each task lists its files, tests (its stop condition) and checks.

**Goal:** the bench project, the scorer that plays the person from an answer key, the report that reads the numbers back from Stats, the in-process replay with its stub model and fixed checks, and the ULB credit card fraud replay. Spec: `docs/superpowers/specs/2026-11-01-stage-7-benchmarks-design.md`. Shared rules and exact interfaces: `docs/superpowers/plans/2026-11-01-stage-7-overview.md` (read it first; it is not repeated here).

**Branch:** `plan/stage-7-bench-core`. **Milestone:** Stage 7: Developer benchmarks. **Consumes:** `main`, with `bench/demo-world` merged (#533). **Produces:** the overview's interface block. The plan PR says `Closes` the plan issue and the task issues still open.

**Read first:** the spec; the overview; `bench/demo-world/src/demo_world/answer_key.py`, `pyproject.toml`; `backend/src/mlops_agents/api/app.py` (`create_app`), `api/security.py` and `api/routers/auth.py` (sign-in, CSRF), `api/routers/{verdicts,baseline,stats,usage,expectations}.py`; `backend/tests/e2e/harness.py` and `fixture_project.py` (how a project, a connection and an approved handoff are set up in-process; read, never import: `tests/` is not packaged).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | The project, CI, `BenchClient`, `DayStarts` | `main` |
| 2 | The scorer | task 1 |
| 3 | The report (early PR) | task 2 |
| 4 | The dataset interface, fixed checks, stub model, replay (early PR) | task 3 |
| 5 | ULB credit card fraud | task 4 |

---

### Task 1: The project, CI, `BenchClient`, `DayStarts`

**Files:** `bench/benchmarks/{pyproject.toml,README.md,uv.lock}`; `bench/benchmarks/src/benchmarks/{__init__,client,dates,cli}.py`; `bench/benchmarks/tests/`; `.github/workflows/benchmarks.yml`; the `AGENTS.md` command line for the bench (one line under the demo-world one, if there is one; else the README only).

**Notes:**
- `pyproject.toml` as the overview's constraints say, with the `bench` console script (`benchmarks.cli:main`). `cli.py` builds its argparse subcommands from `COMMANDS` (overview), so each later task or plan adds one entry and edits nothing else.
- Paths given to `BenchClient` are the app's routes without `/api` (`/projects/...`, `/usage`, `/auth/token`); the client adds the prefix.
- `sign_in` reads `MLOPS_AGENTS_ACCESS_TOKEN`; missing → an error saying which variable, never echoing a value. Both clients keep the session and `csrf_token` cookies and send `X-CSRF-Token` on POST. An HTTP error raises with the route and status, never the request body.
- `business_date`: `starts` ascending; an `at` before the first start maps to the first date.

**Tests (stop condition):** `in_process` signs in to a `create_app` app on a temporary `MLOPS_AGENTS_HOME` and GETs `/api/projects`; a POST carries the CSRF header (a route that requires it succeeds); `sign_in` without the variable fails naming it; `business_date` on a day start, inside a day, before the first start, after the last; ruff, pyright, tests; the new workflow green on both runners.

---

### Task 2: The scorer

**Files:** `bench/benchmarks/src/benchmarks/scorer.py`; its tests.

**Notes:**
- The rules are the spec's "The scorer" section, one function each where they are not one line: `problems` (groups `Record` lines by `kind`, `start`, `end`; skips every other line kind); the business date of an incident from its `detected_at`; matching (from the day before `start` to `end + GRACE_DAYS`; among candidates, a problem whose `[start - 1 day, end]` holds the date beats one reached only by grace days, then the incident's own category, then the earliest); the verdict values; misses (one per unmatched problem, `occurred_at` = its start's day start); silent team alerts (by `ran_at` through `business_date`).
- Before writing anything: `GET /projects/{id}/verdicts` and `/misses`; any verdict with `source == "user"` or any miss → `AlreadyScored`. A `labels` verdict is overwritten. Merged incidents go to `Score.excluded`.
- Incidents are read with paging (`GET /projects/{id}/incidents` until a short page), never one capped request.

**Tests (stop condition):** against an in-process app with incidents raised through `Services` (as the e2e harness does), at fake-clock times: an incident in a problem's window of its category → `real_issue`; of another category → `wrong_type` with the problem's category; outside every window → `not_an_issue`; a problem on two columns caught by one incident → no miss; an uncaught problem → one miss at its start; an incident detected the day before a problem's start matches it; one detected `GRACE_DAYS + 1` after its end does not; a date inside one problem's window and in another's grace days goes to the first; a merged incident is excluded; a project with a user verdict → `AlreadyScored`, nothing written; a `labels` verdict is replaced; a silent team alert inside a window → `real_issue`, outside → `not_an_issue`; `CiFailureEvent` and `PullDecision` lines are ignored.

---

### Task 3: The report (early PR)

**Files:** `bench/benchmarks/src/benchmarks/report.py`; `cli.py` (`bench report`); its tests.

**Notes:**
- What each mode shows, and how each number is labelled, is the spec's "The report" section; the per-mode group lists live in one constant in `report.py`. Values come from `/stats?window=all` and `/stats?window=all&category=<c>` for every category with at least one problem, printed with their `n` and "collecting" / "not available" exactly as `StatValue` says; a group the mode cannot fill is listed as "not measured in this mode".
- Usage (agents mode): `GET /usage?since=<run start date>`; the run's app has its own data folder, so app-wide usage is the run's. Tokens and cost per agent and model and in total (`UsageRow` has no request count, so requests are not reported; an unpriced model's cost is "not available"), the run's real start, end and duration, and every `Pause` with its length.
- `report.json` holds everything `report.md` shows (the `StatsView` objects as returned, the `Score`, the `RunInfo`); `bench report` builds its table from `report.json` files only.

**PR:** open a PR into `main` for tasks 1 to 3 as soon as this task is done, from `plan/stage-7-bench-core-task3` (the agents plan consumes the client, `DayStarts`, the scorer and the report).

**Tests (stop condition):** a replay-mode report never prints effort, cost, model impact or reliability values and lists them as not measured; an agents-mode report omits `alerts_per_week` and `time_to_resolution_s` and labels the pickup delay; a replay-mode report labels diagnosis "category of the check that fired"; a `collecting` value prints as collecting, a `None` as not available, never 0; usage and pauses appear in agents mode; `bench report` on two `report.json` files gives one table; no access token in either file (a test sets one and greps).

---

### Task 4: The dataset interface, fixed checks, stub model, replay (early PR)

**Files:** `bench/benchmarks/src/benchmarks/{datasets/__init__,datasets/contract,checks,stub_model,replay,fake_clock}.py`; `cli.py` (`bench replay`); a tiny fixture dataset under `bench/benchmarks/tests/`; its tests.

**Notes:**
- **The stub model** is an OpenAI-compatible chat-completions endpoint the replay starts on `127.0.0.1` (a free port) for the run's length and registers as a `self_hosted` provider with one model (`POST /api/providers`, then the model, then `verify`); every reply is a final assistant message with no tool call, so each agent turn ends at once. It is the bench's own code, a few dozen lines; the backend's test fakes are not imported or copied.
- **The fixed checks** are pure functions from a day's frames (and the reference frames they need) to `ResultRecord`s with `origin: "agent"`, `ran_at` = the day's start, `category` per the spec's list; `accuracy` writes `origin: "team"` and is the dataset's team check. A dataset says which checks it runs and on which columns (its `handoff()` registers each as a check with a daily cadence and a file results location). Thresholds are named constants in `checks.py`.
- **The replay's clock** (`fake_clock.py`, the bench's own; the e2e harness's is in `tests/`): `now()` returns the set time; `sleep()` yields once and returns. After the lifespan starts, the replay stops the background scheduler (`services.scheduler.stop()`; `run_due` does not depend on the loop) and drives `services.scheduler.run_due` itself, once per day, so nothing runs beside it and nothing waits on the clock.
- **The replay** builds `create_app` with that clock, a fresh `MLOPS_AGENTS_HOME` under the run folder and a fixed `MLOPS_AGENTS_ACCESS_TOKEN` it generates for the run, and runs its lifespan in-process (`app.router.lifespan_context(app)`, as `backend/tests/api/conftest.py`'s `started_app` does); sets the project up through `Services` (project: a small local git repository the replay creates under the run folder, since the e2e's fixture repository is in `tests/`; local-folder connection on the data folder, the dataset's handoff approved, the parser for its results files approved); then for each day: `write_day`, set every file it wrote to the day's start as its modification time (`os.utime`; feeds are judged on file times, and a wall-clock time would make every feed look silent), set the clock to the day's start plus one hour, `run_due`; then `score` with the day starts, then `write_report` with `mode="replay"`.

**PR:** open a PR into `main` for tasks 1 to 4 as soon as this task is done, from `plan/stage-7-bench-core-task4` (the datasets plan consumes the interface, the checks and the replay).

**Tests (stop condition):** the stub answers a chat completion with a final message and no tool call; a replay of the fixture dataset (a few days, one null spike) opens one `data_quality` incident, scores it `real_issue`, makes no request beyond `127.0.0.1`, and writes both report files; each fixed check fires on a crossing input and stays quiet on a normal one; a second `bench replay` into the same run folder refuses (the scorer's `AlreadyScored`); the fixture's feed raises no silence on a normal day (file times follow the fake clock); CI green.

---

### Task 5: ULB credit card fraud

**Files:** `bench/benchmarks/src/benchmarks/datasets/ulb.py`; `bench/benchmarks/scenarios/ulb.toml`; its tests (on a small synthetic frame with ULB's columns, never the download).

**Notes:**
- Fetch: OpenML 1597 (`creditcard` v1) parquet from its pinned URL, checked against a pinned SHA-256 (record the hash in the module after downloading it once; the OpenML md5 `178bcf9bb1f31a3dfe12d0e577884add` is of the ARFF file, not the parquet). Cite the dataset and its licence (OpenML: "Public"; the original's page lists DbCL 1.0) in the README.
- Days: rows in `Time` order spread evenly over 90 business days from a fixed start date (stated in the report's dataset line as synthetic). `DATASETS` gets its `ulb` line.
- The bench's model: LightGBM trained on the first 20 days, scoring each later day; `pr_auc_matured` over its scores with labels available the next day.
- Scenario (`ulb.toml`, demo-world's scenario format where its kinds fit, the bench's own kinds otherwise, each appending its `Record`): at least 5 `data_quality` problems (null spikes, out-of-range `Amount`), at least 5 `drift` problems (covariate shifts in `Amount` and a few `V*` columns), and one fraud pattern the model never saw (`model_performance`), all in the 70 scored days, 3 days each, each starting at least 9 days after the previous one of its category ends. Injections are defined in `ulb.py` (not demo-world's format, which works on the hotel bookings frame).
- Run it once for real and put its `report.md` in the plan PR's description.

**Tests (stop condition):** the day split keeps every row once, in `Time` order; no two problems of one category have overlapping match spans; a scenario injection changes only its window's days and appends its `Record`; the model trains and scores on the synthetic frame; the fetch refuses a file whose hash differs; CI green (no download in CI).

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
