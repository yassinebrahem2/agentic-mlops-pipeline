# Stage 7: Developer Benchmarks Design

**Status:** draft, 2026-11-01. In scope for 2026-11-05 (the user, 2026-11-01), with the agents mode (the user: the demo world merges by 2026-11-02; plan on it being merged).

## Goal

Measure the app against problems whose answers are known, with the numbers the product's Stats page already computes: how often an incident is a real problem, how many problems are missed, how noisy the alerts are, how often bad data and drift are told apart. The answer key plays the person: instead of a person judging each incident, the bench judges it from the key through the product's own verdict routes, then reads the product's own Stats. This is the roadmap's "same engine as Stats, with injected answer keys instead of user verdicts".

**Non-goals:** nothing in the product changes; the bench is never shown to users and never shipped in the wheel; no metric is defined outside the product (a number Stats does not compute is left out, or added to the product later by its own Decision); no comparison across models in v1; the demo world's planted flaws are not scored by Stats (the agents report lists them as a checklist a person ticks against the agents' audit reports).

**Anti-goal:** a number that looks like agent quality when no agent ran, or that blames the agents for a quota stall. Every report names its mode in its title, and an agents run with a stall says so in its title.

## Where it lives

`bench/benchmarks/`: its own uv project beside `bench/demo-world/`, with its own CI workflow (`.github/workflows/benchmarks.yml`: push-only on `main` and `plan/**`, path-filtered, Ubuntu and Windows, ruff, pyright strict, tests on small fixtures; the ULB download and the full datasets run only in a real benchmark run, never in CI). It depends on `backend/` (`mlops-agents`) and `bench/demo-world/` (`demo-world`) as path dependencies, and on `river` (BSD 3-Clause). Nothing in `backend/` imports it.

## What it reuses, and from where

| Thing | One home | The bench uses it to |
|---|---|---|
| Answer-key line `Record {kind, category, column, start, end}` and `answer_key.jsonl` | `demo_world.answer_key` (`Record`, `lines`, `append_new`) | write every dataset's key; the scorer reads only `Record` lines (the file also holds `CiFailureEvent` and `PullDecision` lines, which are not problems) |
| Verdicts and misses | the product's routes: `POST /projects/{id}/incidents/{id}/verdict`, `POST /projects/{id}/misses`, `POST /projects/{id}/baseline/silent-alerts/{result_id}/verdict` | play the person |
| The numbers | `GET /projects/{id}/stats` (`StatsView`, I41) | fill the report |
| Stalls | `GET /projects/{id}/activity?type=turn.stalled` | mark a run that stalled |
| Sign-in | `POST /api/auth/token` with `MLOPS_AGENTS_ACCESS_TOKEN` (local mode); then the session cookie and the `X-CSRF-Token` header | reach a running app; the token is read from the environment only, never logged or written to a report |
| The fake clock and due jobs | `create_app(clock=...)`, `Scheduler.run_due` | drive the replay |
| A model endpoint | a `self_hosted` provider (`POST /api/providers`; a `http://127.0.0.1:<port>/v1` base URL is accepted and needs no key) | point the replay's agents at the bench's stub |

The scorer's access to the app is HTTP only (its other input is the date map, below), so it scores a running app (agents mode) the same way as the replay's in-process app; it never reads `app.db` or `project.db`. The replay sets its project up in-process through the app's services (project, local-folder connection, a fixed handoff with its checks and a file results location, a team source), as the stage 4 end-to-end test does, because a handoff otherwise needs an agent to propose it.

## Two run modes

1. **Replay (deterministic).** The app runs in-process with a fake clock, its data folder in a temporary `MLOPS_AGENTS_HOME`. For each business day the bench writes that day's data files and check results, sets the clock to that day, and calls `run_due`. The checks are a fixed generic set the bench computes and writes as `ResultRecord`s (with `ran_at` = the day) to a file results location: null share and range per column, a population-stability drift score per feature, PR-AUC over matured labels; a legacy accuracy check is written as the team source. Agents are woken as usual and answered by the bench's own stub: a minimal OpenAI-compatible endpoint, registered as a `self_hosted` provider, whose every reply ends the turn without acting, so no request leaves the machine (written for the bench; the backend's test fakes are not packaged and are not copied). **It measures the checks and the incident machinery (grouping, severity, silence, reopening), not agent judgment**, and its report says so.
2. **Agents (the demo world with a real model).** A demo-world evaluation run (`demo-world new-run`, then `up`) against a running app with a model the person chooses, then scored by the same scorer against the run's answer key. It measures the agents' work: the checks they register, their diagnoses, the incidents they open. It needs no change to the demo world: the company's daily job already writes `data/jobs/<business date>.jsonl` with each job's real `started` time, failed jobs included, so the scorer's date map is the earliest `started` of each business date. **It depends on two fixes**, hard Consumes of the agents plan: the demo company writes each check line's real run time as `ran_at` (plan #643; today the lines carry no time, so no parser can give them a correct one), and the product tells agents that `ran_at` is when the check ran (bug #645; today it says "the time the result describes", so the agents' own checks would stamp business dates). With a business date as `ran_at`, every check with a cadence raises a false silence each real day and the run's precision would measure a clock bug. Without both, the agents mode is out of the 2026-11-05 scope. The run can start once #533 and both fixes are merged, before any Stage 7 code exists.

   **A short run, not the demo's nine months** (the user, 2026-11-01). The agents run plays its own scenario, `bench/benchmarks/scenarios/agents-short.toml` (demo-world's scenario format, read by demo-world's own parser), from 2016-07-01: a quiet first week, then one window at a time, each 3 days long and starting 9 days after the previous one ends (beyond the 7-day lookbacks of the company's cancellation-rate and drift checks, as `default.toml` spaces them), and the run ends 8 days after the last window. The windows: 5 `data_quality` problems (null spikes and out-of-range values, each on its own column) and one `score_failure` (`operational`). No `schema_break`: a bookings file with a missing column makes the company's scoring fail, so a correct "failed scoring" detection would score as `wrong_type`. The scenario file states the exact dates.

   **The run waits before day 1 until the agents are ready.** The world trains and deploys the first model and backfills the history, then holds before the first simulated day with MLflow still served. Onboarding and the first audits run on the history; the person running the benchmark approves Onboarding's proposals (connections, parsers, the handoff) and merges the first-audit PRs by hand, as in a live demo (the world's merger counts simulated days, so it cannot merge while the world holds). "Ready" is: every agent's handoff turn has ended and no agent PR is open; it does not require any check, so a run whose agents register none still starts, and what they then miss is a real miss. Then the day loop starts with `--merge-after-days` for later PRs, and the quiet week is the agents' checks' baseline. The report records how long the wait lasted and that the first-audit PRs were merged by hand.

   **Pauses never raise alarms.** The app's clock runs on while the world holds (before day 1, or on a stall), so every pause is covered by a project-wide maintenance window opened through the app's own route (`POST /projects/{id}/maintenance-windows`) and ended on resume; silence is suspended inside it. Holding the day loop while MLflow stays served needs one demo-world change (today `up` stops the server with the loop), a task of the agents plan after #533 merges.

   **Model and keys.** The run uses the person's model through the providers registry with all their keys on that provider (up to four Gemini keys, 2026-11-01); the registry moves to the next key when one is exhausted, and a stall comes only when every key is. Gemini's limits apply per Google Cloud project, not per key (Google's rate-limits page: "Rate limits are applied per project, not per API key"), so more keys add quota only when each comes from its own project. **A stall pauses the run instead of losing it:** the agents-run command watches for `turn.stalled` and holds the world; the person presses Try again once quota is back, and the command resumes the world on `turn.resumed` (it never retries by itself); the report records each pause, and no window played during a pause exists, so none becomes a miss. Nothing about the run's requests, tokens or duration is estimated in advance: the report measures them from the app's own usage records (requests, tokens, per agent) and the run's real start and end.

   **Pace.** The pace is a run parameter (`--seconds-per-day`), recorded in the report. The app reads results, syncs tracking and evaluates expectations every 60 s. What a fast pace makes undetectable is anything judged against real-time deadlines (a daily cadence spans many simulated days) and windows of a few real minutes, so the short scenario injects no missing feed and no `ci_failure`; every kind it injects is detected from a file's or a job's content (silence during the run's own pauses is suspended, above).

## The scorer (both modes)

`score(client, project_id, key, day_starts) -> Score`, once per project, after the run:
- It refuses a project that already has a person's verdict (`source: "user"`) or a miss (a second scoring would double `misses`, which is a count). A verdict confirmation by labels wrote during the run is replaced by the scorer's (the store keeps one verdict per incident). Merged incidents are skipped, as Stats skips them.
- **The date map** is the list of business-day starts: in the replay, the fake clock's days; in agents mode, the earliest `started` of each business date in the company's `data/jobs/<date>.jsonl`. A business date with no jobs line (the world failed before the daily job ran) takes the previous day's start, and the report counts these gaps.
- **Business date of an incident** = the day whose start is the last one at or before its `detected_at` (the app's clock in both modes; in the replay the day starts are the fake clock's days). `detected_at` is used, not `started_at` (the first result's `ran_at`), so one rule serves both modes.
- An incident **matches** a `Record` when its business date falls between the day before the line's `start` (the world writes a day's files before the company's daily job starts, so an early detection maps to the previous day) and its `end` plus a grace of 7 days (a check over matured labels fires after the problem). Among several problems it could match, one whose own window `[start - 1 day, end]` holds the date beats one it reaches only by grace days; then the one of the incident's category; then the earliest. Scenarios space problems of one category so their match spans never overlap (each starts at least 9 days after the previous one of its category ends). The column is not used (an incident carries no subject of its own). This matching rule is the bench's own; it is not the product's Baseline rule, which ties a team fire to an incident by cause key.
- A matched incident of the line's category gets `real_issue`; of another category, `wrong_type` with the line's category; an unmatched incident gets `not_an_issue`.
- **One problem, one count.** `Record` lines with the same `kind`, `start` and `end` are one problem (demo-world writes one line per column): an incident matching any of them matches the problem, and a problem no incident matched becomes one miss, `occurred_at` = its `start` day start. Scenarios are sized in problems, since `STATS_MIN_VERDICTS` counts verdicts.
- A silent team alert is judged `real_issue` when its business date (its `ran_at` through the date map) falls in a `Record`'s span plus grace, else `not_an_issue`.
- Then it reads `/stats` (window `all`), overall and per category, and the stall events.

## The datasets and their scenarios

Each replayed dataset is played day by day; every injection appends its `Record`. Each scenario has at least `STATS_MIN_VERDICTS` (5) lines in every category it reports, or the report shows that category as blank by design.

| Dataset | Source and licence | Days | Injected |
|---|---|---|---|
| ULB credit card fraud | OpenML 1597 (`creditcard` v1; licence field "Public", checked 2026-11-01; Kaggle's original page lists DbCL 1.0, unverified), fetched at run time from a pinned URL and SHA-256, never committed | its `Time` covers 2 days, so rows are spread in order over 90 synthetic business days (the report says so) | null spikes and out-of-range values in `V*` and `Amount` (data quality); a fraud-rate shift and a covariate shift in `Amount` (drift); a fraud pattern the bench's model (LightGBM, trained on the first 20 days, scoring the other 70) never saw (model performance); the legacy accuracy check stays quiet under extreme imbalance, as the demo's does |
| Synthetic drift | river's `ConceptDriftStream` (SEA or Agrawal, a seed per scenario) for P(y\|x) changes, and a seeded covariate shift for P(x) changes | 90 days | drift points at chosen positions and widths (each a `drift` or `model_performance` line), plus null spikes |
| Pipeline telemetry | generated by the bench (no public dataset has per-run job records: SMD and NAB are machine metrics, SKAB is GPL-3.0; the user agreed 2026-11-01) | 60 days | through the product's real operational paths only: a feed that misses its deadline (silence), files that arrive late, a malformed results file (parser failure). Failed CI or training runs need a tracking source, whose fakes are test-only: left out |
| Hotel demo world | `bench/demo-world`, the bench's short scenario | 73 business days, 2016-07-01 to 2016-09-11 (7 quiet; 6 windows of 3 days, each starting 9 days after the previous ends; 8 days after the last) | as the short scenario says (above); agents mode only |

## The report

One Markdown and one JSON file per run, in the run's folder (outside the repo). The title names the mode, and an agents run lists each pause (the readiness wait, and every model stall with how long it lasted). Then: dataset, scenario, seed, app commit and, in agents mode, provider and model; the key's `Record` count per category; the Stats groups that mean something in that mode (replay: detection, noise, speed printed as "pickup delay: first firing result to incident", and diagnosis printed as "category of the check that fired", since no agent recategorizes anything in the replay; agents: detection, noise without `alerts_per_week`, diagnosis, effort, cost, and the pickup delay under the replay's label; `alerts_per_week` and `time_to_resolution_s` are not measured in agents mode, because they run on real time while a simulated day lasts only the run's pace; the report adds the run's measured requests, tokens and duration), each with its `n`, and "collecting" or "not available" where Stats says so. Groups the mode cannot fill are listed as not measured, never as zero. In agents mode, the planted flaws as a checklist. `bench report <run>...` puts several runs in one table.

## Cut order (first to go)

1. The agents run itself (the scorer and the date map stay, so a run can be scored later).
2. Pipeline telemetry.
3. Synthetic drift.
4. ULB stays: the roadmap's first named dataset and the extreme-imbalance case.

## Plans

Three plans in the Stage 7 overview, with exact Consumes and Produces; each consumes `demo_world.answer_key` (on `main` once #533 merges) and otherwise only `main`:
- **Bench core** (`plan/stage-7-bench-core`): the project and CI; the scorer and the date map (an early PR, consumed by the other two); the report; the replay with the stub and the fixed checks; the dataset interface (early PR); ULB and its model.
- **Bench datasets** (`plan/stage-7-bench-datasets`): synthetic drift and telemetry on the dataset interface.
- **Bench agents run** (`plan/stage-7-bench-agents`; also consumes plan #643, bug #645's fix and #533): demo-world holding its day loop with MLflow served; the agents-run command (the wait before day 1, stall pauses, maintenance windows); the short scenario; the date map from the company's jobs files, the scorer's command against a running app, the run's report with the planted-flaws checklist and the scenario kinds the run's pace makes undetectable (kept out of the misses), and the run.
