# Stage 7: Bench Agents Run Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. Four tasks, one PR for the branch, no early PR (`AGENTS.md` rule 5). Each task lists its files, tests (its stop condition) and checks.

**Goal:** one short benchmark run with real agents on the demo world, scored by the core's scorer and reported, that loses nothing to the agents' setup time or to a model stall. Spec: `docs/superpowers/specs/2026-11-01-stage-7-benchmarks-design.md` ("Two run modes", 2). Shared rules and interfaces: `docs/superpowers/plans/2026-11-01-stage-7-overview.md`.

**Branch:** `plan/stage-7-bench-agents`. **Milestone:** Stage 7: Developer benchmarks. **Consumes:** bench core task 3's early PR (`BenchClient`, `DayStarts`, `score`, `write_report`); `bench/demo-world` merged (#533); the demo repo's plan #643 merged (monitoring lines carry `ran_at`); task #647 merged (agents are told `ran_at` is when the check ran). **Produces:** `day_starts_from_jobs` (overview). The plan PR says `Closes` the plan issue and its task issues.

**Read first:** the spec; the overview; `bench/demo-world/README.md` and `src/demo_world/{up,simulate,cli,newrun,merger}.py`; the demo overview's data layout (`docs/superpowers/plans/2026-10-27-demo-overview.md`); `backend/src/mlops_agents/api/routers/{incidents,expectations,activity,approvals}.py` (maintenance windows, expectations, `turn.stalled` / `turn.resumed`).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | demo-world holds its day loop with MLflow served | #533 |
| 2 | The short scenario and the day starts from the jobs files | core task 3; task 1 |
| 3 | The agents-run command | task 2; #643; #647 |
| 3b | The run is safe to score (pre-run review, 2026-11-03) | task 3 |
| 4 | The run and its report | task 3b |

---

### Task 1: demo-world holds its day loop with MLflow served

**Files:** `bench/demo-world/src/demo_world/{up,simulate,cli}.py`; `bench/demo-world/README.md`; their tests.

**Notes:**
- `up` gains a hold: a file in the world folder, `hold`, that the day loop checks before each day; while it exists the loop waits (MLflow stays served, nothing is written), and it continues when the file is removed. With `--hold-before-start`, `up` creates it after the first model is trained and deployed and the history is backfilled, before the first simulated day. The `--seconds-per-day` pace restarts from the resume (a hold is not a day).
- A hold never changes what a day writes or the answer key; a held run that is stopped and started again resumes as today.

**Tests (stop condition):** with the hold file present the loop plays no day and the server keeps answering; removing it plays the next day; `--hold-before-start` holds after the first model exists and before day 1's files; demo-world's tests and CI green.

---

### Task 2: The short scenario and the day starts from the jobs files

**Files:** `bench/benchmarks/scenarios/agents-short.toml`; `bench/benchmarks/src/benchmarks/jobs_days.py` (`day_starts_from_jobs`); their tests.

**Notes:**
- The scenario is the spec's, with exact dates: 2016-07-01 to 2016-09-11, quiet 07-01 to 07-07, then six 3-day windows each starting 9 days after the previous ends (07-08, 07-19, 07-30, 08-10, 08-21, 09-01; the last ends 09-03): five `data_quality` problems, each on its own column (null spikes and out-of-range values; no `schema_break`, spec), and one `score_failure`. demo-world's scenario format, loaded by demo-world's own parser.
- `day_starts_from_jobs(jobs_dir)`: for each `<business date>.jsonl`, the earliest `started` (UTC) of its lines; the dates in the run's range with no file or no line are returned as gaps, and `business_date` then falls back to the previous day's start (the spec).

**Tests (stop condition):** demo-world's parser loads the scenario and every window falls inside the run; `day_starts_from_jobs` on a fixture folder gives each date's earliest start, a date with no file as a gap, and a replayed day (lines appended twice) still its earliest start.

---

### Task 3: The agents-run command

**Files:** `bench/benchmarks/src/benchmarks/agents_run.py`; `cli.py` (one `COMMANDS` entry, `bench agents-run`); its tests.

**Notes:**
- `bench agents-run --run <demo-world run> --project <id> --base-url <app>` signs in (`sign_in`), then drives the run in this order:
  1. **Wait before day 1.** The world is started by the person with `up --hold-before-start --seconds-per-day <pace> --merge-after-days <n> --scenario agents-short.toml`. The command polls (every 30 s) until "ready" (spec): a handoff exists (`GET /projects/{id}/handoff`), no agent's session has a running or queued message (`GET /projects/{id}/agents/{agent}/session` for each of `GET /projects/{id}/agents`), no approval is pending (`GET /projects/{id}/approvals`), and no agent PR is open (`GET /projects/{id}/github/activity`, `kind: "pr"`, `state: "open"`); then removes the hold. The person approves Onboarding's proposals and merges the first-audit PRs meanwhile; the command prints what it is waiting for, nothing else.
  2. **Play, holding on a stall.** It watches `GET /projects/{id}/activity?type=turn.stalled` (and `turn.resumed`). On a stall it creates the hold file and records a `Pause("model_stall", ...)`; it removes the hold only when no `kind: "stall"` approval is pending any more (`GET /projects/{id}/approvals`; when all keys are exhausted every agent mid-turn stalls, each resumed by the person's Try again). It never retries a turn itself.
  3. **No silence during a hold.** For every hold (before day 1 included) it keeps a maintenance window open: every 5 minutes while held, `POST /projects/{id}/maintenance-windows` with `start` = now, `end` = now + 10 minutes, `scope` = every expectation's ref from `GET /projects/{id}/expectations`, `reason` = "benchmark hold"; when there is no expectation yet, no window is opened. Windows are never deleted (deleting one would let the held slots be judged afterwards).
  4. **Score and report** when the world's state file shows the last day done: `day_starts_from_jobs` on the run's `data/jobs`, the answer key's lines, `score`, then `write_report` with `mode="agents"`, the pauses, the day gaps, the pace, the provider and model (read from the app), and the app's commit.
- The run's app is started by the person on its own `MLOPS_AGENTS_HOME` for this run (the report's usage is app-wide).

**Tests (stop condition):** against an in-process app and a fake world folder: the command holds before day 1 and releases only when ready; a `turn.stalled` event creates the hold; with two stalls pending, resolving one keeps the hold and resolving both removes it; while held, windows cover every expectation and a held slot raises no silence (evaluate across a held hour); the pauses reach the report; scoring runs once, after the last day.

---

### Task 3b: The run is safe to score

The pre-run review of tasks 1 to 3 (2026-11-03) found defects that would lose or bias the run. This task fixes them before task 4; nothing here changes the backend or the scorer.

**Files:** `bench/benchmarks/src/benchmarks/agents_run.py` and its tests; `bench/demo-world/src/demo_world/up.py` (the stale hold, the dead cutoff code), `bench/demo-world/README.md`; `bench/benchmarks/README.md` (the runbook).

**Notes:**
- **First merge `origin/main`** into the branch: the run's app must start from the merged head (#720 grouping, #721 results re-reading, #722 sandbox limits, #723 token counts).
- **The result is never lost.** Read every piece of the report's metadata (agents, providers, app commit) before `score()`; `_get_app_commit` never raises (unknown when git cannot say). Write the score result to the run folder as soon as `score()` returns, then the report.
- **A person's verdict is caught early.** A person who confirms or labels an incident during the run makes `score()` refuse (`AlreadyScored`, merged core, unchanged). The run polls for verdicts during play and prints a warning the moment one appears. Before scoring it refuses with the list of judged incidents, after the metadata is saved.
- **Stalls are dated by the stall.** Seed the seen stall events at the end of the readiness wait, so a stall during onboarding is not a pause. A stall's window starts at the stall event's `ts` (a window may start in the past). Poll every `min(30 s, pace / 4)`.
- **Windows end soon after a resume** (the spec: windows are ended on resume; the maintenance routes have no update, and windows are never deleted). Each window lasts twice the renewal interval (renewal `min(30 s, pace / 4)`), and is renewed while the hold lasts, so after a resume at most half a simulated day stays suspended. `suspended()` also mutes real fires, so this bounds the bias.
- **Readiness from the sessions, never from errors.** Ready when each of the four audit agents' sessions has the handoff message and a finished turn after it, and none is running. A route error counts as not ready: it is printed and retried, never read as idle.
- **The answer key is read by demo-world's own reader** (`answer_key.lines()`, bench already depends on demo-world). A line it cannot read stops the run.
- **Play ends when the agents are done.** After the last day, wait until every agent is idle and no stall is open, up to a timeout (a setting). A timeout is recorded in the report.
- **demo-world `up`:** a `hold` file left by an earlier run is removed at start, unless `--hold-before-start` creates it again. Remove the dead state-derived cutoff code. The README says `backfill` comes before `up`.
- **Runbook** (`bench/benchmarks/README.md`, "The agents run"), in order:
  1. `new-run`, then `backfill`.
  2. `up --hold-before-start` with the pace, the merge delay and `scenarios/agents-short.toml`.
  3. The app on a dedicated `MLOPS_AGENTS_HOME`.
  4. `bench agents-run`, with the same pace and scenario as `up` and the access token.
  5. While it runs: approve Onboarding's proposals, merge the first-audit PRs by hand, press Try again on every stall, give no verdict or label, keep the machine awake.

**Tests (stop condition):**
- A failure after `score()` (an unreachable `/providers`) still leaves the score result and a report on disk.
- A person's verdict during play is warned about, and scoring then refuses and names it.
- A stall raised during onboarding is not a pause.
- A stall's window starts at its event's time.
- After a resume, no window outlives twice the renewal interval.
- A route error during readiness keeps waiting.
- A malformed answer-key line stops the run.
- Scoring waits for agents still in a turn, and records a timeout.
- A stale `hold` file is removed.
- `uv run pytest`, ruff, ruff format and pyright in `bench/benchmarks` and `bench/demo-world`; CI green.

### Task 4: The run and its report

**Files:** none in code; the plan PR's description.

**Notes:** run it once on a fresh demo-world run (`new-run`) with the person's model and keys, at the pace they choose, after #643 and #647 are merged and the demo repo's copy has #643's change. Put the run's `report.md` in the plan PR's description with the demo-world run name, the company commit and the app commit, and its `report.json` in a collapsed block below it. If a run is lost to something outside the bench (a crash of the app or the world), say so in the PR and run again; do not tune the scenario after the run.

**Tests (stop condition):** the report exists and every number in it comes from the run (the reviewer checks `report.json` against the PR's table).

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
