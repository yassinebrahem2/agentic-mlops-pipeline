# Stage 6 — Stats Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; tasks 1 and 2 also get early PRs (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, and its tests (the task's stop condition).

**Goal:** a person judges every incident (real issue, not an issue, wrong type) and reports what the app missed; labels confirm or refute model performance incidents on their own; the team's own monitoring is kept as the baseline, linked to each incident, and its alerts the agents stayed silent on are listed for verdicts; the Stats page shows how the agents do against that baseline, per category and time window, each number with what it rests on and "collecting data" below 5 verdicts (S4 §10, roadmap stage 6, `project_knowledge.md` Process 2026-10-29).

**Branch:** `plan/stage-6-stats`. **Overview:** [stages 4 to 6](../2026-10-29-stage-4-6-overview.md). Read it first (global constraints, I22 to I42, "Routes", "Files shared by these plans"); this plan does not repeat it.

**Consumes:**
- contracts task 1 (not on `main`): I31 (`VerdictValue`, `VerdictDraft` and its validator, `VerdictView`, `MissReport`, `BaselineLink`, `SilentVerdict`, `SilentTeamAlert`, `Verdicts`, `Baseline`), I41 (`STATS_MIN_VERDICTS`, `StatsWindow`, `StatGroup`, `STAT_METRICS`, `StatValue`, `StatsView`, `Stats`), `cause_key`, `ACTIVE_STATUSES`, `MonitoringSettings`, `QueryProviders`, `untrusted`, the event types `verdict.recorded` and `miss.reported`, the packages `monitoring/verdicts/` and `stats/`.
- contracts task 2 (not on `main`): the tables `verdicts`, `silent_alert_verdicts`, `misses`, `baseline_links`.
- incidents task 1 (not on `main`): `Incidents` (`get`, `list`, `signals`, `close`, `add_observer`, `bind_verdicts`), `IncidentView`, `SignalView`, `IncidentObserver`, `IncidentChange`.
- results task 2 (not on `main`): `Results` (`query` with `ids`, `add_observer`), `ResultQuery`, `StoredResult`, `ResultsObserver`. Results task 3: `Scheduler.add_sync_observer`, `SyncObserver` (task 1's label confirmation runs in `AFTER_SYNC`).
- hooks task 1 (not on `main`; task 3): `Hooks.runs`.
- ui task 1 (early PR; task 3's cost group): `cost_usd` and `ModelEntry.price` (`providers/usage.py`, I42).
- On `main`: `Clock` (I21), `SqliteEventLog`, `Databases`, `Handoffs` (I20), the stage 2 tables `approvals`, `tasks`, `turns`, `messages`, `llm_calls`, `events`; `ProvidersService` (the model list).

**Produces:**
- `SqliteVerdicts(Verdicts)` with `record`, `get`, `report_miss`, `list`, `confirm_by_labels`; `LabelConfirmation(SyncObserver)`; the verdict and miss routes; the `verdicts` query provider; `IncidentView.verdict` bound (task 1, early PR; ui task 3 builds the verdict buttons and the miss report on the routes).
- `SqliteBaseline(Baseline)` with `available` and `judge_silent`, a `ResultsObserver` and an `IncidentObserver` filling `baseline_links`, the baseline, silent-alert and silent-alert verdict routes (task 2, early PR; the Dashboard reads `available`).
- `StatsService(Stats)` and `GET /api/projects/{id}/stats?window=&category=` returning `StatsView` (task 3, I41).
- The Stats page (task 4).

## Tasks

| Task | Title | Consumes | Starts |
|---|---|---|---|
| 1 | Verdicts, miss reports, confirmation by labels, routes, the `verdicts` provider | contracts tasks 1, 2; incidents task 1; results tasks 2, 3 | after incidents task 1 and results tasks 2 and 3; **early PR** |
| 2 | The baseline record, `available`, the silent-alerts list and its verdicts | task 1; results task 2; incidents task 1 | after task 1; **early PR** |
| 3 | The Stats computation and route (I41) | tasks 1, 2; hooks task 1; ui task 1 | after task 2, hooks task 1 and ui task 1 |
| 4 | The Stats page | tasks 2, 3 | after task 3 |

**One source per rule (settled here):**
- `monitoring/verdicts/baseline.py` holds `team_fire_matches(result, incident, window_minutes) -> bool`, the one rule that ties a team fire to an incident; links, silent alerts (task 2) and `caught_baseline_missed` (task 3) all use it.
- `stats/sources.py` holds every read stats makes of stage 2's tables (`approvals`, `tasks`, `turns`, `messages`, `llm_calls`, `events`): read-only SQL, one function per number. No other stats file queries a table it does not own. Stage 4-5 data (incidents, results, verdicts, baseline, hook runs) is read only through their protocols.
- `stats/metrics.py` holds one function per metric of `STAT_METRICS`; `stats/service.py` only windows, filters and assembles. Metric names come only from `STAT_METRICS`; the frontend renders what the API returns.

---

### Task 1: Verdicts, miss reports, confirmation by labels, routes, the `verdicts` provider

**Files:** new `backend/src/mlops_agents/monitoring/verdicts/store.py` (`SqliteVerdicts`), `monitoring/verdicts/labels.py` (`LabelConfirmation`), `monitoring/verdicts/query.py` (the provider), `backend/src/mlops_agents/api/routers/verdicts.py`; `api/services.py` (one `verdicts` field, its construction, `incidents.bind_verdicts(verdicts)`, `add_sync_observer`, the provider); tests under `backend/tests/monitoring/verdicts/`; regenerated `frontend/openapi.json`.

**Interfaces:**
- Consumes I31's types, `Incidents.get`, `Incidents.list`, `Incidents.signals`, `Incidents.close`, `Incidents.bind_verdicts`, `Results.query`, `EventLog.record`, `Clock`, `SyncObserver`, `QueryProviders`.
- Produces `SqliteVerdicts(databases, incidents: Incidents, results: Results, events: EventLog, clock: Clock)` implementing `Verdicts`, `LabelConfirmation(verdicts).synced(project_id, now)`, the `verdicts` provider (`what="verdicts"`; filters `incident_id`, `since`; `limit` max 200), and the routes of the overview's "Routes" row for stats task 1:
  - `POST /api/projects/{id}/incidents/{incident_id}/verdict`, body `VerdictDraft`, answers `VerdictView`; 404 `incident_not_found`; 422 from `VerdictDraft`'s validator.
  - `GET /api/projects/{id}/verdicts?since=`, answers `list[VerdictView]`, newest first.
  - `POST /api/projects/{id}/misses`, body `{description: str (1..2000 chars), category: ResultCategory | None, occurred_at: datetime}`, answers 204; `reported_at` is the server's clock, never the body's.
  - `GET /api/projects/{id}/misses?since=`, answers `list[MissReport]`, newest first.

**Notes:**
- `by` is the literal `"user"`, as the approval decision route records its actor; verdicts are a person's act, never a tool. The verdict's `source` is `user` here.
- `check_versions`: the stored results behind the incident's signals (`Results.query(ResultQuery(ids=[...result_ids]))`), `check_id -> version` for those whose `version` is set.
- One verdict per incident: a new user verdict replaces the stored one; each recording writes `verdict.recorded` (subject the incident, payload the value and source). A `labels` verdict never replaces a `user` verdict.
- Closing: a user verdict on a `resolved` incident closes it through `Incidents.close(project_id, incident_id, by)` (S4 §5: "closed after the verdict", and the verdict is the person's act); on any other status it is recorded and the status is unchanged. A `labels` verdict never closes. Nothing else here changes an incident.
- `report_miss` writes `miss.reported` (no subject). Description text is data: stored and shown, never passed to an agent except through `untrusted()`.
- **Confirmation by labels** (`confirm_by_labels`, run by `LabelConfirmation` after each sync): candidates are `model_performance` incidents with a model, no `user` verdict, not `closed` or `merged`. The evidence: `agent`-origin `model_performance` results for the incident's model with `not_enough_data = false` and `ran_at` after the incident's `started_at` (filter `category` in code; `ResultQuery` has no category field); `not_enough_data` records are the minimum sample not being met and never decide. The latest such result per check: any fired → `real_issue`; none fired → `not_an_issue`. Recorded with `source = "labels"`, the checks' versions, `verdict.recorded`. A changed outcome replaces the earlier `labels` verdict; the same outcome writes nothing.
- The provider's rows reach the agent through the `MonitoringQuery` tool's `untrusted()` rendering; a verdict's `note` is free text, never instructions.

**Tests (stop condition):** a verdict on a resolved incident is stored, closes it and writes one `verdict.recorded`; on an open incident it is stored and the status stays; a second verdict replaces the first; `wrong_type` without a right category and `real_issue` with one are 422; an unknown incident is 404; `check_versions` holds the versions of the incident's agent checks; `IncidentView.verdict` shows the verdict once bound; a miss is stored with the server's `reported_at` and listed; `list(since)` filters; labels: enough fired label results confirm `real_issue`, non-fired ones refute, only `not_enough_data` records decide nothing, a user verdict is never replaced, a second sync with no new results writes no event, non-model-performance incidents are untouched, a labels verdict never closes; the provider filters and pages and a note carrying an instruction stays inside the untrusted block; the routes need a session; `openapi_export --check` passes; full suite, ruff, format, pyright; CI green.

**PR:** early PR from `plan/stage-6-stats-task1` into `main` (`Closes #<task issue>`): ui task 3 consumes these routes and the bound verdict.

---

### Task 2: The baseline record, `available`, the silent-alerts list and its verdicts

**Files:** new `monitoring/verdicts/baseline.py` (`team_fire_matches`, `SqliteBaseline`, its two observers), `api/routers/baseline.py`; `api/services.py` (the observers' `add_observer` lines, one `baseline` field); tests; regenerated `openapi.json`.

**Interfaces:** consumes `ResultsObserver`, `IncidentObserver`, `Results.query`, `Incidents.list`, `cause_key`, `MonitoringSettings.grouping_window_minutes`, `EventLog`. Produces `SqliteBaseline(Baseline)` (`available`, `links`, `silent_alerts`, `judge_silent`) and the routes `GET /api/projects/{id}/incidents/{incident_id}/baseline` (`list[BaselineLink]`), `GET /api/projects/{id}/baseline/silent-alerts` (`list[SilentTeamAlert]`), `POST /api/projects/{id}/baseline/silent-alerts/{result_id}/verdict` (body `{value: SilentVerdict}`, answers `SilentTeamAlert`).

**Notes:**
- Never notifies, never raises a signal, never opens an incident (S4 §10); it only writes `baseline_links` and `silent_alert_verdicts`.
- `team_fire_matches`: a `team`-origin fired result matches an incident when `cause_key(result.subject, result.model) == incident.cause_key` and `ran_at` lies in `[incident.started_at − grouping_window_minutes, end]`. The window's end (Decision #565, one helper beside the matcher): a live incident (status in `ACTIVE_STATUSES`, read from the incidents constant) ends now, even with an old `resolved_at` (a reopened one); otherwise `resolved_at`, else `closed_at` (a person can close an open incident). A merged incident keeps matching on its own cause key (`merge` moves its signals to the survivor but not its key), up to its survivor's end by the same rule, and the link goes to the survivor (`merged_into`), never to the merged incident. The matcher takes the survivor when there is one.
- Two directions, both through the matcher, both idempotent on (incident, team check): the `ResultsObserver` links newly stored team fires to matching incidents; the `IncidentObserver` on `opened` and `reopened` looks back for team fires already stored. `first_fired_at` is the earliest matching fire; `lead_seconds = (first_fired_at − incident.detected_at)` in seconds (negative: the team fired first).
- `silent_alerts`: team fired results that match no incident, newest first, capped at 500, each with its stored verdict. `judge_silent` stores a person's verdict on one (`by="user"`, replaced on a second call) and writes `verdict.recorded` with the result as subject; a result that is not a silent team alert is 404.
- `available`: true once the project has at least one stored `team`-origin result; the Dashboard (ui task 2) and Stats read it, nothing else decides it.
- Both observers sit on paths that must not fail: the `IncidentObserver` is awaited inside `raise_signal` (paging), the `ResultsObserver` beside incidents' own observer during delivery. Incidents and results already isolate observer failures; each baseline observer also catches its own errors, logs them at warning with the IDs and returns, so a baseline bug never delays a page or an incident.

**Tests (stop condition):** Decision #565's cases: an incident a person closed while open stops matching after `closed_at`; a reopened incident with an old `resolved_at` still matches a fire now; a fire on a merged incident's cause key, inside its survivor's window, links to the survivor; a team fire before an agent incident gives a negative `lead_seconds`, after it a positive one; a team fire on another cause key or outside the window links nothing; a repeated delivery or a reopen adds no second link; a team fire never raises a signal or a notification (assert no `signal.raised`, no `notification.*` event); an unmatched team fire is listed as silent and drops off once an incident matches it; a silent-alert verdict is stored, shown in the list and replaced by a second one; `available` flips with the first team record; an error inside either baseline observer fails neither `raise_signal` nor the delivery; full suite and checks; CI green.

**PR:** early PR from `plan/stage-6-stats-task2` into `main` (`Closes #<task issue>`): ui task 2 (the Dashboard) reads `Baseline.available`.

---

### Task 3: The Stats computation and route (I41)

**Files:** new `stats/sources.py`, `stats/metrics.py`, `stats/service.py` (`StatsService`), `api/routers/stats.py`; `api/services.py` (one `stats` field); tests under `backend/tests/stats/`; regenerated `openapi.json`.

**Interfaces:** consumes `Verdicts.list`, `Baseline` (`available`, `links`, `silent_alerts`), `Incidents.list`, `Results.query`, `Hooks.runs`, `cost_usd` and `ModelEntry.price` (ui task 1), `Clock`. Produces `StatsService(Stats)` and `GET /api/projects/{id}/stats?window=7d|30d|all&category=` (`StatsView`; a bad `window` or `category` is 422).

**Notes:**
- Computed on read, nothing stored. Whether the team caught an incident comes from `Baseline.matched_incidents` (added to I31 on 2026-10-31, implemented in this task in `monitoring/verdicts/baseline.py` on the same matcher as `silent_alerts`, reading every team fire page by page), never from the stored `baseline_links`: an attach that moves `started_at` earlier, or an `update` back to a live status, widens the window without re-linking. A fire that matches a merged incident counts for its survivor (merged incidents are left out of Stats). The stored links only feed the incident page. The window is `[now − 7d or 30d, now]` on the incident's `detected_at` (and on each other row's own time); `all` has no lower bound. `merged` incidents are left out everywhere (they were absorbed). `category` filters incidents by their current category; groups not built from incidents ignore it.
- `collecting = n < STATS_MIN_VERDICTS` only for verdict-based numbers (marked V below); then `value` is still computed but the page shows "collecting data". Counts have `collecting = false`. With `baseline_available = false`, the numbers that need the baseline (B) are `value = None`, never 0.
- Definitions (n in brackets):

| Metric | Value |
|---|---|
| precision (V) | `real_issue` ÷ verdicts on incidents [verdicts] |
| caught_baseline_missed (V, B) | `real_issue` incidents not in `Baseline.matched_incidents` [verdicts] |
| false_alarms_avoided (V, B) | silent team alerts judged `not_an_issue` [those verdicts] |
| misses | miss reports + silent team alerts judged `real_issue` [count] |
| detection_delay_s (V) | median `detected_at − started_at` of `real_issue` incidents [verdicts] |
| time_to_resolution_s (V) | median `resolved_at − detected_at` of resolved `real_issue` incidents [verdicts] |
| alerts_per_week | incidents opened ÷ weeks in the window [incidents] |
| signals_per_incident | mean `len(signal_ids)` [incidents] |
| bad_data_vs_drift_correct (V) | of `data_quality` and `drift` incidents with a verdict: `real_issue` ÷ (`real_issue` + `wrong_type`) [those verdicts] |
| autonomous_share | `Hooks.runs` with basis `rule` or `policy`, plus holds (kind `hold`, tier 1), ÷ all hook runs [runs] |
| approvals_requested, acceptance_rate | approvals created; approved ÷ decided [approvals] |
| prs_opened, prs_merged, prs_closed | agents' tasks with a PR, by `created_at` / `ended_at` and status [count] |
| metric_before, metric_after (V) | mean of the latest label-based result value before `detected_at` and after `resolved_at`, over resolved `real_issue` model performance incidents [those incidents] |
| tokens_per_agent | window's prompt + completion tokens ÷ agents with calls (the per-agent split is Usage's) [agents] |
| cost_per_incident | window's dollars (`cost_usd` with each model's price) ÷ incidents opened; `None` when a used model has no price [incidents] |
| failed_runs, tool_errors, sandbox_failures | turns `failed`; `tool_result` parts with `is_error`; `sandbox.build.failed` events [count] |

- `cost_per_incident` takes dollars only from `cost_usd`; stats never multiplies a price itself.
- A metric whose rows are empty is `value = None, n = 0`, never 0.

**Tests (stop condition):** a team fire in the part of a window widened by an attach (`started_at` moved earlier) keeps the incident out of `caught_baseline_missed`; a team fire on a merged incident's cause key keeps its survivor out of it; a fixture project (real stores on a temp data folder, fake `Clock`) with known incidents, verdicts, team fires, silent-alert verdicts, hook runs, approvals, tasks, turns and `llm_calls` gives the expected value and n for every metric in `STAT_METRICS` (the test iterates `STAT_METRICS`, so a metric without a function fails); 4 verdicts → `collecting` true, 5 → false; no team records → `baseline_available` false and the B numbers `None`; `7d` excludes an 8-day-old incident and `all` includes it; a merged incident is not counted; `category` narrows the incident groups only; an unpriced model gives `cost_per_incident = None`; bad query values are 422; full suite and checks; CI green.

---

### Task 4: The Stats page

**Files:** new `frontend/src/pages/stats/route.tsx` (path `/projects/:projectId/stats`, nav area `project`, label "Stats"), `StatsPage.tsx`, `metricLabels.ts` (display label and unit per metric name, falling back to the raw name), `SilentAlerts.tsx`, their tests.

**Interfaces:** consumes `GET .../stats`, `GET .../baseline/silent-alerts` and `POST .../baseline/silent-alerts/{result_id}/verdict` through the generated `schema.d.ts` and TanStack Query.

**Notes:**
- Window picker (7 days, 30 days, all) and category picker (all, or one `ResultCategory` from the schema); both in the query key.
- Each number shows its value with unit (durations from seconds, shares as percent, dollars) and "based on n"; when `collecting` is true it shows "collecting data (based on n)" from the API's `collecting`, never a copied 5.
- `baseline_available` false: a notice says the team's own monitoring is not readable yet, so comparisons with it are unavailable (S4 §10), and the B numbers show "not available", not 0.
- The silent-alerts list (check, subject, time, verdict) with two buttons per row (real issue, not an issue); a verdict refetches the list and the stats.
- The page iterates the response's groups and metrics; it hardcodes no metric list.

**Tests (stop condition):** renders every group and metric of a sample `StatsView`; `collecting` shows "collecting data"; `None` shows "not available"; the baseline notice appears when false; changing the window refetches with the new parameter; a silent-alert verdict posts the right body and refetches; loading and error states; `npm test`, `npm run lint`, `npm run format:check`, `npm run build`; CI green.

---

## Known debt added

- `stats/sources.py` reads stage 2 tables (`approvals`, `tasks`, `turns`, `messages`, `llm_calls`, `events`) directly, read-only, because their owners have no aggregate read; the reads sit in that one module so a later read API replaces only it. Recorded in `docs/known-debt.md` with the plan PR.
