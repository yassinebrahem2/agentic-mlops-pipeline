# Stage 4 — Results Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. One PR for the branch, plus the early PRs marked below (`AGENTS.md` rule 5). Each task lists its files, interfaces, tests (its stop condition) and checks.

**Goal:** every result the app can read becomes one stored standard result record, read on the app's own scheduler, once and in order, also after downtime; every feed, job and check the team or an agent declared has a deadline, and a miss is a silence signal; agents register the checks they deliver by PR (S4 §2, §3, §3a, §3b, §4, §13).

**Branch:** `plan/stage-4-results`.

**Read first:** the [stages 4-6 overview](../2026-10-29-stage-4-6-overview.md) (do not expect it repeated here): I22, I24, I25, "Shared by contracts", "Routes", "Files shared by these plans", decided points 1, 19, 21; S4 §2, §3, §3a, §3b, §4, §13, §14; `tracking/service.py` (`run_sync`, `query`, `sync_interval`), `tracking/loop.py` (the model for a loop, removed by task 3), `agents/pr_sync.py` (`sync_now`), `api/security.py` (`_OPEN`), `api/app.py` (`create_app`).

**Consumes:**
- contracts task 1 (early PR): I22 (`ResultRecord`, `StoredResult`, `ResultQuery`, `SourceState`, `CollectionReport`, `source_id`, `parse_source_id`, `ResultsObserver`, `Results`, `ParserSpec`, `ParserVersion`, `ParseOutcome`, `Parsers`, `EvaluationCopies`); I24 (`JobKind`, `JOB_ORDER`, `JobDue`, `HeartbeatView`, `SyncObserver`, `ScheduledJob`, `Scheduler`); I25 (`ExpectationKey`, `ExpectationSpec`, `ExpectationState`, `CheckView`, `SECOND_DEADLINE_KEY`, `Maintenance` and `NeverSuspended`, `Expectations`); `SignalDraft` (I26); `MonitoringSettings`; `HandoffObserver` and `Handoffs.add_observer`; `PrObserver`, `PrChange`, `PrSyncObserver.add_observer`; `QueryProviders`, `QueryProvider`, `MonitoringQuery`; the `res` and `chk` prefixes; the events `results.collected`, `check.registered`, `parser.disabled`; the `RegisterCheck` name; `croniter`.
- contracts task 2 (early PR): the tables `results`, `result_sources`, `parsers`, `expectations`, `scheduler_jobs`, `checks`; `get_monitoring_settings`.
- contracts task 4 (task 6): `Condition`, `evaluate`, `PARSER_KIND`, `ParserPayload`.
- incidents task 1 (early PR; tasks 4, 5, 6): `Incidents.raise_signal`. Results raises only the silence, source-failure and parser-failure signals (I22).
- hooks task 2 (early PR; task 6): `build_one_shot_run_args` (the one-shot container run).
- On `main`: `Clock`, `SystemClock` (I21, `providers/failover.py`); `Cadence`, `Feed`, `CheckSpec`, `FileResults`, `CiResults`, `MlflowResults`, `OnCallHours`, `Operations` (I19, `proposals/payloads.py`); `Handoffs.current` (I20); `TrackingService.run_sync`, `.query`, `.sync_interval`, `EvaluationRecord`, `Page` (I16); `PrSyncObserver.sync_now`; `Connections.list`, `.inspect`, `.tables`; `ProposalKind`, `ProposalKinds.register` (I19); `Sandbox`, `DockerRunner`; `WorkspaceService.list_projects`.

**Produces:** `AppScheduler` (I24's `Scheduler`, with `add_sync_observer`), the times functions `next_occurrences`, `in_hours`, `local_day_start`, and the `clock` parameter of `create_app` and `build_services` (task 1); `Results` (task 2); the `SYNC_TRACKING`, `SYNC_PRS`, `AFTER_SYNC` jobs and `GET /api/health/heartbeat` (task 3); `EvaluationCopies` and `COLLECT_RESULTS` (task 4); `Expectations`, `CHECK_EXPECTATIONS`, `GET .../expectations`, the `results` and `expectations` query providers (task 5); `Parsers` and the `parser` kind and card (task 6); the `RegisterCheck` tool, the checks store, `GET .../checks`, the `checks` provider (task 7). Every type is contracts'.

## Tasks

| Task | Title | Consumes | Starts |
|---|---|---|---|
| 1 | Scheduler loop, job table, catching up, times and DST, the clock | contracts tasks 1, 2 | after contracts tasks 1, 2; **early PR** |
| 2 | The record, `Results`, idempotent ingest and delivery, query, the `file` source, cursors | contracts tasks 1, 2 | after contracts tasks 1, 2; **early PR** |
| 3 | `sync_tracking`, `sync_prs`, `after_sync`, the heartbeat route | task 1 | after task 2; **early PR** |
| 4 | `ci` and `mlflow` sources, `collect_results`, the evaluation copy, source failures | tasks 1, 2; incidents task 1 | after task 3 and incidents task 1 |
| 5 | Expectations and silence, `check_expectations`, the expectations route | tasks 1, 2; incidents task 1 | after task 4; **early PR** |
| 6 | Parsers (mapping and code), the `parser` kind and card | task 2; contracts task 4; hooks task 2 | after task 5, contracts task 4 and hooks task 2 |
| 7 | `RegisterCheck`, the checks store and route, activation on the PR's merge | tasks 2, 5 | after task 6 |

Core path: tasks 1 to 5; extras: 6 and 7. Task 3 needs only task 1, so the `SyncObserver` seam reaches `main` without waiting on incidents. Every early PR comes from `plan/stage-4-results-task<n>` at that task's last commit.

**Where shared things live (one source each):** job kinds and order: `monitoring/jobs.py`; setting defaults: `MonitoringSettings`; every type: the contract files (all contracts'). This plan's own constants: `monitoring/scheduler/constants.py` (`SCHEDULER_TICK_S = 5`), `monitoring/results/constants.py` (`QUERY_LIMIT_MAX = 500`, `DETAIL_MAX_BYTES`). Source IDs are built only by `source_id()`; the expectation key of a source or check is built only in `monitoring/expectations/keys.py`.

---

### Task 1: Scheduler loop, job table, catching up, times and DST, the clock

**Files:** new `backend/src/mlops_agents/monitoring/scheduler/{constants,times,store,loop}.py`; `api/app.py` and `api/services.py` (`clock: Clock | None = None` on `create_app` and `build_services`, `SystemClock()` when none, kept on `Services.clock`; construct the scheduler, `stack.push_async_callback(scheduler.stop)`, `await scheduler.start()` after every `register`, just above the wake-ups' start); tests under `backend/tests/monitoring/scheduler/`.

**Interfaces:** consumes `JobKind`, `JOB_ORDER`, `JobDue`, `ScheduledJob`, `SyncObserver`, `Scheduler`, `Clock`, `Cadence`, `OnCallHours`, `MonitoringSettings.timezone`, the `scheduler_jobs` table. Produces `AppScheduler(Scheduler)` (`__init__(databases, projects: Callable[[], Awaitable[list[str]]], clock: Clock, sleep=asyncio.sleep)`; `add_sync_observer` keeps the observers in registration order for task 3's `AFTER_SYNC` job), `next_occurrences`, `in_hours`, `local_day_start` in `times.py` exactly as I24, and `Services.clock`, which every stage 4 to 6 service takes.

**Notes:**
- `run_due(project_id, now)`: for each registered job in `JOB_ORDER`, `after` is the latest due already stored for that kind (none yet: `now` minus one interval, so a new project does not backfill history); insert each `JobDue` from `job.due(after, now)` (unique kind, due, subject: inserting twice is a no-op); then run every unfinished row in `JOB_ORDER`, then by due time. A row is finished only after `run` returns; a job that raises gets its error (scrubbed) on the row and is not retried; the others still run. A row started but not finished (the process died) runs again at the next start: jobs are idempotent.
- The loop: every `SCHEDULER_TICK_S`, every project, one project's failure never stops another; `stop()` cancels like `TrackingLoop.close`. The scheduler never runs a check or a model.
- Interval jobs (this plan's five, and the other plans' interval jobs) put their dues on a UTC grid of the interval and, on catching up, return only the latest grid point in `(after, now]`: each reads from its cursor, so one run covers the gap. A provided helper `interval_dues(interval_s, after, now)` in `times.py` is the one implementation of that rule.
- Times: `croniter` in the cadence's IANA zone (`zoneinfo`; `tzdata` is a dependency), results as UTC. DST: a local time that does not exist is skipped, one that occurs twice fires once (the first). `every_minutes` is a UTC grid from the Unix epoch. `in_hours`: an end before the start crosses midnight; the day is the start's day.

**Tests (stop condition)** (fake clock: `FakeClock` from `tests/providers/fake_clock.py`): jobs run in `JOB_ORDER` then due time, with two fake jobs; a missed batch after a gap of hours runs each interval kind once; a raising job is recorded and the next still runs; a row left started is run again after a restart and a finished one is not; two `run_due` calls in a row run nothing twice; a daily 02:30 cron in `Europe/Paris` across the March change skips that day and across the October change fires once; `every_minutes` across a DST change stays every N minutes in UTC; `in_hours` for a window crossing midnight; `local_day_start` in a non-UTC zone; `create_app(clock=fake)` hands the fake to the scheduler; ruff, format, pyright; CI green.

**PR:** its own early PR into `main` (`Closes #<task issue>`); every other plan registers its job on it.

---

### Task 2: The record, `Results`, idempotent ingest and delivery, query, the `file` source, cursors

**Files:** new `monitoring/results/{constants,store,service,file_source}.py`; `api/services.py` (one `results` field and its construction, the startup delivery); fixture folders under `backend/tests/fixtures/results/`; tests under `backend/tests/monitoring/results/`.

**Interfaces:** consumes the I22 types, `Handoffs.current`, `Connections.list` and `.inspect`, the `results` and `result_sources` tables, `new_id("res")`, `EventLog`. Produces `ResultsService(Results)`; `results.collected` once per `collect` with the `CollectionReport` counts.

**Notes:**
- **Sources** are derived on each `collect` from the current handoff (`Operations.results_location`, each `CheckSpec.results`, each job `Feed`'s `JobFeedRef.file`) and, from task 7, the active checks; `result_sources` keeps only their state. Tasks 4 and 6 add the other kinds and the parsed sources through one `SourceReader` registry in `service.py`.
- **Origin is the source's, never the line's:** the results location and registered checks' files hold standard records (origin `agent` as the check writes it; any other origin there is an invalid line); a team source's parsed records get `team` (task 6); `ci` and `mlflow` get `tracker` (task 4). In this task a source that needs a parser has none yet, so it is not read and its cursor stays at 0: nothing is lost, task 6 reads it from the start.
- **File reading (costly):** the connection must be a `local_folder`; the pattern is matched under its `path` only, every resolved file (symlinks too) must stay under that root, and a pattern with `..` or an absolute path is refused. Files the connection kind's secret-file rule flags (`Findings.secret_files`, from `Connections.inspect` once per collection) are never opened: that rule is reused, never restated. Cursor per file: size, modified time, byte offset; a file that shrank or whose modified time changed while its size is below the offset is read again from the start; only whole lines up to the last newline are consumed. Source key: the record's own key when the line has one, else `<file>:<offset>`.
- **No data loss, no duplicates (costly):** a source's new records and its cursor are written in one transaction; insert is `ON CONFLICT (source_id, source_key) DO NOTHING`, and only rows actually inserted are returned. `collect` reads every source oldest first, then delivers all new records, sorted by `ran_at`, to the observers once (catching up raises the night in order).
- **Delivery (costly: a lost delivery is a lost night incident):** each observer is awaited in registration order and isolated (one that raises is logged with the project and the others still run); once all have returned, the batch's `delivered_at` is set. At startup and at the start of each `collect`, results with `delivered_at` null are delivered first, oldest `ran_at` first. Observers are idempotent by result ID (incidents' signals are unique by result), so a re-delivery after a crash changes nothing twice.
- A line that fails validation is recorded on the source (`consecutive_failures`, `last_error` scrubbed) and the cursor moves past it, so one bad line never blocks the source; it is never dropped silently: task 4 raises a signal for it. `detail` is stored as given, capped at `DETAIL_MAX_BYTES`; it is data, never read as instructions.
- `query`: filters of `ResultQuery` (`ids` included), newest `ran_at` first with the ID as tie-break, `latest=true` keeps the newest per check and model; `since`/`until` inclusive on `ran_at`; `limit` above `QUERY_LIMIT_MAX` is refused. `latest_per_check(subject, model)`: the newest record of each check for that subject and model.

**Tests (stop condition):** a fixture JSON Lines file becomes standard records; collecting twice stores nothing twice; an appended file is read from its offset; a truncated or rewritten file is read from the start and stores only new keys; a half-written last line waits for its newline; the cursor survives a new service instance (restart); two sources with interleaved times reach the observer in `ran_at` order, once; results committed but not delivered (an observer that crashes the test process is simulated by not calling it) are delivered at the next start, once; a raising observer does not stop the next and the batch is marked delivered; an invalid line is recorded on the source and does not stop the next lines; a pattern with `..`, a symlink out of the root and a `.env`-style file the pattern matches store nothing; query filters, `ids`, `latest`, paging over 120 rows, limit 501 refused; `latest_per_check`; ruff, format, pyright; CI green.

**PR:** its own early PR into `main` (`Closes #<task issue>`): incidents task 1 subscribes to `Results`.

---

### Task 3: `sync_tracking`, `sync_prs`, `after_sync`, the heartbeat route

**Files:** new `monitoring/scheduler/sync_jobs.py`; new `api/routers/heartbeat.py`; `api/security.py` (one `_OPEN` entry); `api/services.py` (register the three jobs; remove `TrackingLoop`'s construction and close); removed `tracking/loop.py` and its tests; regenerated `frontend/openapi.json`; tests.

**Interfaces:** consumes `TrackingService.run_sync` and `.sync_interval`, `PrSyncObserver.sync_now`, the observers of `Scheduler.add_sync_observer` (task 1), `HeartbeatView`. Produces the `SYNC_TRACKING`, `SYNC_PRS`, `AFTER_SYNC` jobs; `GET /api/health/heartbeat` → `HeartbeatView`.

**Notes:**
- `sync_tracking` awaits `run_sync` (never the background `sync`), so `ci` and `mlflow` sources are current before collection and silence are judged; due on the tracking sync interval grid (`sync_interval`). It replaces `TrackingLoop`, which is removed so tracking is not synced twice; the tracking routes' own "sync now" stays. `sync_prs` awaits `sync_now`; it and `after_sync` are due on the `collection_interval_s` grid. `after_sync` runs every registered `SyncObserver` in registration order, each isolated: one raising is logged and recorded on the job row, the rest still run.
- **Heartbeat (costly: it is the one unauthenticated route):** `GET` only; no session, no CSRF; takes no input, reads nothing, changes nothing, returns exactly `{"ok": true}`. The Host check applies as for every route; notifications task 3 adds the `public_url` host for this path.

**Tests (stop condition):** catching up runs `sync_tracking`, `sync_prs`, a fake `COLLECT_RESULTS` job, `after_sync` in that order (recording fakes); a raising sync observer does not stop the next; nothing syncs tracking but the job (no `TrackingLoop` left); the heartbeat answers without a cookie, refuses `POST`, refuses a foreign Host, and every other `/api` path still needs a session; `openapi_export --check` passes after the regeneration commit; ruff, format, pyright; CI green.

**PR:** its own early PR into `main` (`Closes #<task issue>`): training, promotion, stats and incidents register sync observers.

---

### Task 4: `ci` and `mlflow` sources, `collect_results`, the evaluation copy, source failures

**Files:** new `monitoring/results/{ci_source,mlflow_source,evaluation_copy,collect_job}.py`; `service.py` (their registry entries, the source-failure signal); `api/services.py` (register `collect_results`, bind `Incidents`); tests.

**Interfaces:** consumes `TrackingService.query` (`ci_runs`, `runs`, `metrics`, `versions`), `EvaluationRecord`, `EvaluationCopies`, `Incidents.raise_signal`, `Maintenance.suspended`, `parse_source_id`, `MonitoringSettings.collection_interval_s`. Produces the two sources, `SqliteEvaluationCopies(EvaluationCopies)`, the `COLLECT_RESULTS` `ScheduledJob`.

**Notes:**
- `ci` (`ci:<workflow>`, one per `CiResults` and per job feed's `ci_workflow`): one record per finished run; failed or cancelled fires; `ran_at` is the run's start plus its duration; source key the run key; category `operational`, subject the workflow, check ID the handoff check's when one names this workflow, else the source ID (incidents' subtype rules and the rules' terms rely on these two). A running run holds the cursor (tracking updates its status in place), so no run is read before it ends.
- `mlflow` (`mlflow:<metric>`): metric points of runs from the cursor's time (`metrics` needs a `run_key`, so runs first); a running run is read again each time, harmless because the key is (run, metric, step, time). Fires by the handoff's `threshold` and `fires_when` (`above`: value > threshold; `below`: value < threshold); without both it is a record that never fires. Model: the model whose version has that run key, else none.
- **Source failures** (S4 §13): a source that cannot be read keeps its cursor and records the error; it, and a line task 2 recorded as invalid, raises a `source_failure` signal (`operational`, subject the source ID, `source_ref` the source ID) through `Incidents.raise_signal`, unless `Maintenance.suspended` holds for the source's connection (`parse_source_id`); incidents' grouping keeps repeats in one incident. `Incidents` is bound after it is built (`bind_incidents`, as `services.py` does with `bind`), since incidents subscribes to `Results`.
- **Evaluation copy:** a line in the results location shaped as an evaluation record (it carries `schema_version` and `evaluation_commit`, not `check_id`) is not a result. It is the `evaluation.json` content plus `model`, `version` and `run_key`. `store_copy` writes it into stage 3's `evaluations` table only when the tracker sync marked that model version `fetch_failed`, under that version's tracking source, and never overwrites a row the sync stored; otherwise it is skipped.

**Tests (stop condition):** using a `TrackingService` filled through an in-test adapter (as stage 3's tests do): a failed and a cancelled run fire with the workflow as subject, a succeeded one does not, a running run is read only once it ends; metric points fire above and below their threshold, none without one, re-reading a running run stores nothing twice; an unreadable source raises a `source_failure` signal (fake `Incidents`) and keeps its cursor, and so does an invalid line (cursor past it); a source whose connection is suspended (fake `Maintenance`) raises nothing; an evaluation copy is stored for a `fetch_failed` version, skipped for a present one, and stored once; `collect_results` due times on the interval grid; ruff, format, pyright; CI green.

---

### Task 5: Expectations and silence, `check_expectations`, the expectations route

**Files:** new `monitoring/expectations/{keys,store,service,seed,job,query}.py`; new `api/routers/expectations.py` (`GET /api/projects/{id}/expectations`); `api/services.py` (construct, `Handoffs.add_observer`, the startup reconcile, `Results.add_observer`, register the job and the two providers); regenerated `frontend/openapi.json`; tests under `backend/tests/monitoring/expectations/`.

**Interfaces:** consumes I25, `HandoffObserver`, `Handoffs.current`, `ResultsObserver`, `Connections.tables`, `TrackingService.query`, `Maintenance` (`NeverSuspended` until incidents task 7), `Incidents.raise_signal` (the incidents service is built first and passed to the constructor; incidents task 3 then binds expectations back with `IncidentsService.bind_expectations`), `next_occurrences`, `MonitoringSettings` (`check_grace_minutes`, `second_deadline_minutes`), `QueryProviders`. Produces `ExpectationsService(Expectations)`, its `HandoffObserver` and `ResultsObserver`, the `CHECK_EXPECTATIONS` job, the route (`list[ExpectationState]`), the `results` and `expectations` query providers.

**Notes:**
- **Seeding** (Decision #448): on `stored(handoff)` and once per project at startup from `Handoffs.current()`: every feed (kind `feed` for data, `job` for jobs, ref the feed's name, its cadence, grace and time zone, `second_deadline=True`) and every `CheckSpec` (kind `check`, ref its `check_id`, `grace_minutes=None`, `second_deadline=False`), source `handoff`; a `handoff`-source key no longer in the handoff is retired (keys from `RegisterCheck` and training are never retired by seeding). Category: data feeds `data_quality`, jobs `operational`, checks their own. Tests call `Handoffs.store` directly.
- **Seen (costly: a wrong time means false silence after downtime):** a stored result marks its key seen at its `ran_at` (`keys.py` maps a source ID or check ID to its key). A data feed is seen at the newest modified time of its table's files (`Connections.tables` → `TableRef.files`, under task 2's root and secret-file rules), read at each check, so data that arrived while the app was down counts at its own time. A `ci_workflow` job feed is seen at the newest `CiRunRow.start` of its workflow, running runs included (`TrackingService.query(what="ci_runs")` at check time), so a job running past its grace is not silent; its result records keep their own time for incidents.
- **Silence (costly: one signal per miss, never zero, never two):** an occurrence `t` is met when a result's `ran_at` falls in `(previous occurrence, t + grace]` (grace `None`: `check_grace_minutes`, read at evaluation). Past `t + grace` unmet: one `silence` signal (detail: the key, `t`, last seen); for `second_deadline` specs, past `t + grace + second_deadline_minutes` still unmet: one more, with `detail[SECOND_DEADLINE_KEY] = True` (severity reads it). The last occurrence signalled at each deadline is stored with the expectation, so catching up raises each missed occurrence once, oldest first. A key `Maintenance.suspended` at `t` raises nothing and shows `suspended`. A `due_at` spec (training's) is one occurrence.
- `states` gives each key's status for the Dashboard and the route: `pending_pr` (inactive), `suspended`, `silent` (a signal raised), `late` (past `t`, inside grace), `due`, `ok`.
- Providers: `results` (`ResultQuery` from the `MonitoringQuery` fields) and `expectations` (the states), each paging with `limit` and `offset`; the tool renders them through `untrusted()`.

**Tests (stop condition)** (`FakeClock`): a handoff stored seeds its feeds and checks, a second one retires a removed feed, startup reconciles a handoff stored before, and a re-store and a restart leave a registered check's and a `training_request` expectation active; a job result inside grace satisfies its occurrence; a missing result past deadline and grace raises one signal and a second run raises none; the second deadline raises one more with the key set, a check raises no second one; after a 10-hour gap each missed occurrence raises once, oldest first, and a feed whose data arrived during the gap raises nothing; a job running past its grace raises nothing; a suspended key raises nothing (a fake `Maintenance`); a `due_at` expectation raises once; a cadence across a DST change misses only real misses; the route lists the states; both providers page; ruff, format, pyright; CI green.

**PR:** its own early PR into `main` (`Closes #<task issue>`): incidents task 3 reads `states`, training registers its `training_request` expectations, notifications and ui read `states`.

---

### Task 6: Parsers (mapping and code), the `parser` kind and card

**Files:** new `monitoring/results/{parsers,mapping,code_parser,parser_kind}.py`; `service.py` (parsed team sources); `api/services.py` (register the kind); `frontend/src/pages/agents/proposals/ParserProposalCard.tsx` and its test; `ApprovalCard.tsx` (one `CARD_FOR_PROPOSAL` line); tests.

**Interfaces:** consumes `ParserSpec`, `ParserVersion`, `ParseOutcome`, `Parsers`, `ParserPayload`, `PARSER_KIND`, `evaluate` (I23), `ProposalKind`, `MonitoringSettings.parser_disable_after`, `Incidents.raise_signal`, `build_one_shot_run_args` (hooks task 2), `Sandbox.rebuild`, `DockerRunner`. Produces `SqliteParsers(Parsers)` and `ParserKind` (key: the source ID; no warnings, no credential slots).

**Notes:**
- A mapping is evaluated in-process: no code, no Docker, no `eval`; fields by JSON path, column or pattern group; `fired_when` only through `expr.evaluate`. A code parser runs in one container of the project's sandbox image (`build_one_shot_run_args` with `network=False`, the connections' read-only mounts and no env file) under its `timeout_s`; Docker down is a failure.
- Every parsed record is validated as a `ResultRecord` with origin forced to `team`; invalid ones are recorded and skipped as in task 2. A parser that raises, times out or cannot run keeps the cursor. Either failure raises a `parser_failure` signal (`operational`, subject the source ID); after `parser_disable_after` consecutive failures the version is disabled with `parser.disabled` and the source waits for a new version, which is read from the cursor.
- `apply` stores the version (idempotent per proposal), enabled; the card shows the source, the mapping or script, the sample input and the sample records.

**Tests (stop condition):** fixture CSV, JSON, JSON Lines and log files parsed by mappings; `fired_when` applied; a new version supersedes; an invalid record is skipped with a signal; a raising parser raises a signal and keeps the cursor; disabled after N failures, read again after a new version; a code parser runs through `FakeDockerRunner` with no network and read-only mounts; apply is idempotent; the card renders its preview (vitest); ruff, format, pyright, eslint; CI green.

---

### Task 7: `RegisterCheck`, the checks store and route, activation on the PR's merge

**Files:** new `monitoring/expectations/checks.py`; new `agents/tools/register_check.py`; `agents/tools/__init__.py` (its `default_registry` lines); `api/routers/expectations.py` (`GET /api/projects/{id}/checks`); `api/services.py` (the `PrObserver` subscription, the `checks` provider); regenerated `frontend/openapi.json`; tests.

**Interfaces:** consumes `CheckView`, the `RegisterCheck` name, `PrObserver`, `PrChange`, `Expectations.register` and `activate_for_pr`, `QueryProviders`. Produces the `RegisterCheck` tool (risk class `read`), the checks store, the route (`list[CheckView]`) and the `checks` provider.

**Notes:** registering stores a `chk` as `pending_pr` and an inactive expectation (source `check`, `activates_on_pr`, `grace_minutes=None`, `second_deadline=False`); re-registering a check ID replaces it; `check.registered` is recorded. A `merged` `PrChange` activates it (also when the merge was seen at night by `sync_prs`); `closed` unmerged retires it. Its results location becomes a source (task 2). Its `version` is the newest record's.

**Tests (stop condition):** register then merge activates the expectation; a merged check that never writes raises a silence signal past its deadline; closed unmerged retires it; the tool refuses a malformed cadence or location; the route and the provider list the check; ruff, format, pyright; CI green.

---

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
