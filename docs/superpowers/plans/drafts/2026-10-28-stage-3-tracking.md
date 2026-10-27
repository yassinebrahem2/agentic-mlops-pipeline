> **Draft.** Missing: the lead's advisor review.

# Stage 3 — Tracking Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works. Read the [stage 3 overview](../2026-10-28-stage-3-overview.md) first: it holds the shared constraints and the interface lists.

**Goal:** the app keeps one record of a project's models, versions, runs, metrics, deployments and CI runs (spec §7):
- tracking sources (MLflow, GitHub Actions) are created through the API, each with at most one credential in the keystore;
- two adapters fill the record over REST, idempotently, keeping the last good data when a source fails;
- a sync loop runs every source on an interval and on demand;
- agents read it through the `TrackingHistory` tool and the `tracking_summary` hook.

**Branch:** `plan/stage-3-tracking`. **Consumes:** I16 (models and protocols, written by contracts task 1), I18 (`HookRegistry`, data-summary task 1), the stage 3 project tables and the `trk` ID prefix (contracts task 1), the stage 2 GitHub client, `KeyStore`, `EventLog`. **Produces:** `Tracking` (I16) and its routes from task 1 (early PR), the fake MLflow server, `TrackingHistory`, `tracking_summary`.

**Read first:** spec §7, §13 to §15 (tracking lines), overview I16 and I18; `integrations/github/api.py` (`request`, `GITHUB_API`), `agents/messaging/wakeups.py` (`start`, `_run_loop`, `close`: the model for the loop), `tests/fakes/fake_github.py` (the model for the fake server), `credentials/scrub.py` (`base_url_problem`, `error_text`, `scrub_values`).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | The tracking service: sources, record, sync, routes | contracts task 1 |
| 2 | The fake MLflow server and the MLflow adapter | task 1 |
| 3 | The GitHub Actions adapter | task 1 |
| 4 | The sync loop | tasks 2 and 3 |
| 5 | `TrackingHistory` and `tracking_summary` | task 1, data-summary task 1 (`HookRegistry`) |

Task 1 gets an early PR, from `plan/stage-3-tracking-task1`: the pages plan reads its routes. Its service starts with no adapter registered (a sync of a source whose adapter is missing records the error "No adapter for this source type"); tasks 2 and 3 each add their one entry in `tracking/adapters/__init__.py`. Tasks 2 and 3 are independent of each other.

**Constants (one source, `tracking/constants.py`, created by task 1):** `SYNC_INTERVAL_KEY = "tracking_sync_interval_s"` and `DEFAULT_SYNC_INTERVAL_S = 300` (the project setting of spec §7); `MIN_SYNC_INTERVAL_S = 30`; `QUERY_LIMIT_MAX = 500`; `TRACKING_REF = "project/{project_id}/tracking/{source_id}"` (the keystore reference of I19, built only by `credentials/project_secrets.py`); `ERROR_MAX_CHARS = 500`. Adapter constants live with their adapter and are named below.

---

### Task 1: The tracking service: sources, record, sync, routes

**Files:** new `backend/src/mlops_agents/tracking/__init__.py`, `tracking/constants.py`, `tracking/sources.py` (source rows, per-type settings models, credential state), `tracking/store.py` (writes and reads of the record), `tracking/service.py` (`TrackingService`, the `Tracking` implementation), `tracking/adapters/__init__.py` (`default_adapters(http, github) -> dict[SourceType, TrackingAdapter]`, empty for now), `backend/src/mlops_agents/api/routers/tracking.py`, `api/services.py` (one `tracking` field and its construction), `events/contract.py` (`"tracking.synced"` in `EventType`, if contracts has not added it), `credentials/project_secrets.py` only if it is not on `main` (see Interface gaps), tests under `backend/tests/tracking/`, regenerated `frontend/openapi.json` and types.

**Interfaces:**
- Consumes I16 in full (`TrackingSourceView`, the row models, `SyncResult`, `TrackingQuery`, `TrackingResult`, `TrackingAdapter`, `Tracking`), `Databases`, `KeyStore`, `EventLog`, `GitHubAuth.token()`.
- Produces `class TrackingService(Tracking)`:
  - `__init__(databases, keystore, events, adapters: Mapping[SourceType, TrackingAdapter], github_token: Callable[[], Awaitable[str | None]], clock: Callable[[], datetime] = utc_now)`;
  - the four I16 methods, plus `set_credential(project_id, source_id, credential: str) -> TrackingSourceView` for the route.
- Produces the routes of I16: `GET` and `POST /api/projects/{id}/tracking/sources`, `POST /api/projects/{id}/tracking/sync` (body `{source_id?: str}`; answers `list[TrackingSourceView]`, the sources after the sync), `GET /api/projects/{id}/tracking/{models|runs|ci-runs|deployments}` (query parameters `model`, `run_key`, `metric`, `since`, `limit`; answers `TrackingResult`), `PUT /api/projects/{id}/tracking/sources/{trk}/credential` (body `{credential: str}`, answers the source view; never echoes the value).
- Produces the event `tracking.synced {source_id, type, ok, versions, runs, metrics, ci_runs, error}` (counts of rows the sync wrote; `error` scrubbed and cut at `ERROR_MAX_CHARS`).

**Implementation notes:**
- **Tables** are the ones contracts wrote in `0004_stage3.sql` and `tables.py` (spec §7 names: tracking sources, `tracked_models`, `model_versions`, `tracked_runs`, `run_metrics`, `deployments`, `ci_runs`). Open a Decision issue instead of editing the migration if a column or key below is missing. The keys this plan relies on: every record row is unique on its source and its ID at the source (`model_versions`: source, model, version; `tracked_runs`: source, run key; `ci_runs`: source, key; `run_metrics`: source, run key, metric, step, time); `tracking_sources` has `last_sync` and `last_error`.
- **Source settings** are validated by one model per type, `extra="forbid"`, in `sources.py`: `MlflowSettings(url: str)` (checked with `base_url_problem`, which already refuses userinfo and non-http(s) schemes; a trailing slash is stripped) and `GithubActionsSettings(repository: str, workflows: list[str] = [])` (`repository` is `owner/name`; `workflows` are workflow file names such as `ci.yml`, empty meaning all). Forbidding extra keys is what keeps a credential out of `settings`: the settings column is stored and returned as given.
- **`auth`:** `mlflow` accepts `none` or `credential`; `github_actions` accepts only `none` (its token is the app's GitHub login, passed to the adapter by the service as `credential`; it has no per-source credential). A `credential` source without a credential set syncs with an error "This source needs a credential" and never sends an unauthenticated request.
- **Credentials** go through `credentials/project_secrets.py` only (`put`, `delete`), with the reference `TRACKING_REF`; no other code calls `KeyStore.set` for a tracking credential. `credential_set` is `KeyStore.get(ref) is not None`; keyring calls run in `asyncio.to_thread`. The route refuses a source that is not `auth="credential"` (422 `tracking_no_credential_needed`). The credential is never in a settings row, an event, a log line, `last_error`, an exception text or a response: `last_error` is passed through `scrub_values(text, [credential])` after `error_text`.
- **Sync of one source** (`_sync_source`):
  1. Hold the source's `asyncio.Lock`; a caller that finds it held awaits the running sync and returns without a second one.
  2. Read the credential, call `adapter.sync(source, credential, since=source.last_sync)`.
  3. An exception or `result.error` → write only `last_error` (scrubbed) and record `tracking.synced` with `ok=false`; rows and `last_sync` stay as they were (the last good data). No partial result is ever written: an adapter returns `error` with empty lists when it fails midway.
  4. Success → one transaction: upsert every row by its key (a repeated sync adds nothing and updates changed fields: run status, version aliases, CI conclusion), make sure a `tracked_models` row exists per model, derive deployments (below), set `last_sync = clock()` and `last_error = NULL`; then record the event.
  - `sync(project_id, source_id=None)` runs every source of the project concurrently with `asyncio.gather` over `_sync_source`, each wrapped so one failing source never stops another. A project with no sources does nothing.
- **Deployments** (MLflow keeps only the current alias, so the app keeps the history): the current state of `(source, model, alias)` is its latest `deployments` row. For each `model -> alias -> version` of `SyncResult.aliases` that differs from the current state, insert a `DeploymentRow(from_version=<current or None>, to_version=version, seen_at=clock())`. A first sync therefore records each existing alias with `from_version=None`. A removed alias is not recorded (the model has no `DeploymentRow` for "unset"); stated in the PR description. `ModelVersionRow.aliases` is overwritten from the same map on every successful sync.
- **Query:** `what` picks the table; filters `model`, `run_key`, `metric`, `since` (on the row's time column); newest first; `limit` is clamped to `QUERY_LIMIT_MAX`; `total` counts all matching rows and `truncated = total > len(rows)`. Rows are the I16 row models dumped with `model_dump(mode="json")`. `models` returns one row per model: `{source_id, model, latest_version, versions, aliases: {alias: version}}`. `metrics` requires `run_key`; without it the service raises `DomainError("tracking_bad_query", ..., status=422)`.
- **Routes** follow `api/routers/sandbox.py` (`ProjectDep`, `ServicesDep`). The `{models|runs|ci-runs|deployments}` path maps to `TrackingQuery.what`; `metrics` and `versions` are reachable only through the tool (the pages need only the four). A missing source is 404 `tracking_source_not_found`; an unknown type or bad settings is 422 with the pydantic message.
- `ids.PREFIXES` must contain `trk` (contracts); the service uses `new_id("trk")`.

**Tests (stop condition)** (with a fake in-test `TrackingAdapter`, a fake keystore, a temp data folder; no HTTP):
- Create: both types round-trip; `mlflow` settings with a userinfo URL, a `file://` URL or an unknown key are refused; `github_actions` with `auth="credential"` is refused; a settings body carrying `token` is refused.
- Credential: `PUT` stores in the keystore and the view flips `credential_set`; the value appears in no response, event, row or log (assert over the stored `settings`, `last_error`, `events` rows and the captured logs); an adapter error that contains the credential is stored scrubbed; the route refuses an `auth="none"` source.
- Sync: first sync writes every row type and one deployment per alias with `from_version=None`; a second identical sync adds no row; a changed alias writes one deployment with the right `from_version`; a changed run status is updated in place; an adapter that raises or returns `error` leaves rows and `last_sync` untouched, sets `last_error`, and a later good sync clears it; two sources, one failing: the other still syncs; two concurrent `sync` calls run the adapter once; a source whose adapter is missing records the error; `tracking.synced` is recorded once per source with the counts.
- Query: each `what`; filters; the clamp, `total` and `truncated`; `metrics` without `run_key` gives 422; an unknown project is 404.
- Routes: the five of I16 incl. the 404 and 422 cases; `npm run gen:api` leaves no diff after the regeneration commit.
- Full suite; `uv run ruff check`, `ruff format`, `pyright`; CI green on every job.

**PR:** open the early PR from `plan/stage-3-tracking-task1` into `main` (`Closes #<task issue>`) as soon as the task is done.

---

### Task 2: The fake MLflow server and the MLflow adapter

**Files:** new `backend/tests/fakes/fake_mlflow.py`, `backend/tests/fakes/test_fake_mlflow.py`, `backend/src/mlops_agents/tracking/adapters/mlflow.py`, `tracking/adapters/__init__.py` (the `mlflow` entry), tests `backend/tests/tracking/test_mlflow_adapter.py`.

**Interfaces:** Consumes `TrackingAdapter`, `SyncResult` and the row models (I16), the app's `httpx2.AsyncClient`. Produces `MlflowAdapter(http: httpx2.AsyncClient)` (`type = "mlflow"`) and `FakeMlflowState`, `create_fake_mlflow_app(state)`, `start_fake_mlflow(state, port=0) -> RunningFakeMlflow`, runnable with `python -m tests.fakes.fake_mlflow --port N` (the same shape as `fake_github.py`).

**Implementation notes:**
- **Fake server** (`/api/2.0/mlflow/...`, in-memory, request log like `FakeGitHubState.requests`):
  - `POST experiments/search`, `POST runs/search` (`experiment_ids`, `max_results`, `page_token`; ordered by start time, newest first), `GET metrics/get-history` (`run_id`, `metric_key`, `max_results`, `page_token`), `GET registered-models/search` (each model carries `aliases: [{alias, version}]` as MLflow 2.3+ does), `GET model-versions/search` (`filter=name='<model>'`), `GET registered-models/alias` (`name`, `alias`).
  - Responses use MLflow's shapes: run `info` (`run_id`, `experiment_id`, `status`, `start_time`, `end_time` in ms and absent while running), `data` (`metrics` with the latest value, `params`, `tags` as key/value lists), `inputs.dataset_inputs`; version fields `name`, `version` (a string), `run_id`, `creation_timestamp`.
  - Paging: `page_size` on the state (default 100, tests set 2) with `next_page_token`. An unknown model or run gives MLflow's error body `{"error_code": "RESOURCE_DOES_NOT_EXIST", "message": ...}` with 404.
  - Scripting: `required_token` (`Authorization: Bearer <token>`, else 401), `fail_next: dict[str, int]` (path suffix to status, one-shot), `aliases_supported: bool` (False omits `aliases` as MLflow before 2.3).
  - Mutators the tests use: `add_experiment`, `add_run`, `finish_run`, `log_metric`, `register_version`, `set_alias`, `delete_alias`.
- **Adapter** (`MlflowAdapter.sync`, REST with the app's client, no MLflow package, `follow_redirects=False` so a credential is never forwarded to another host, 30 s timeout, `Authorization: Bearer <credential>` only when a credential is given):
  1. `experiments/search` (paged) gives experiment ID to name.
  2. `runs/search` over all experiment IDs, newest first, at most `MLFLOW_RUN_CAP = 500` runs, paged by 100. A source with more runs keeps its 500 newest; stated in the PR description.
  3. A run is "changed" when its status is `RUNNING` or `SCHEDULED`, or its `end_time >= since - MLFLOW_OVERLAP` (`MLFLOW_OVERLAP = 1 day`), or `since is None`. Metric history (`metrics/get-history` per metric key of the run, at most `MLFLOW_SERIES_PAGES = 10` pages of 1,000 points) is read only for changed runs; the upsert makes re-reading harmless.
  4. `registered-models/search` (paged) for the model names and `aliases`; `model-versions/search` per model for versions.
  5. Mapping: `RunRow.key` is `run_id`; `status` is MLflow's status lowercased; `start` and `end` are ms to UTC datetimes; `git_commit` is the tag `mlflow.source.git.commit`; `datasets` are the `dataset_inputs` names; `params` and `tags` values are cut at 500 characters and tags at 100 per run; `MetricPoint.time` from the point's timestamp; `ModelVersionRow.run_key` from `run_id` (null when empty); `aliases` in `SyncResult` is `model -> alias -> version` from the models' alias lists, and each version row's `aliases` is the inversion of it.
  - The `registered-models/alias` endpoint is not needed: the search response carries every alias with its version, so the adapter makes no per-alias call (the fake still serves it, as the MLflow client uses it, so a test can check it agrees with the search).
  - A server older than MLflow 2.3 sends no `aliases`: the adapter returns versions and runs and no aliases (so no deployments); not an error.
- **Errors:** every failure returns `SyncResult(versions=[], runs=[], metrics=[], aliases={}, ci_runs=[], error=<plain message>)`: unreachable (`httpx2.HTTPError` through `error_text`), 401 and 403 ("The tracker rejected the credential"), any other non-200 ("MLflow answered with HTTP <n>"), a body that does not parse ("MLflow answered with data this app cannot read"). Messages never contain the URL's query, the credential or a response body.
- Windows and Linux: timestamps are integers in ms; the fake binds `127.0.0.1` and picks a free port; nothing depends on the OS.

**Tests (stop condition):**
- The fake server (own tests): paging with `page_size=2` returns every item once; bearer auth gives 401 without the token; `fail_next` fires once; `aliases_supported=False` omits the field; the alias endpoint agrees with the model search.
- The adapter against the fake: an experiment with a finished and a running run, a model with two versions and an alias, metric series with several steps: every mapping above (status, ms to datetime, git commit tag, datasets, truncation of long values, alias inversion); paging across all five calls; with `since` set after the finished run's end, history is read only for the running run (assert on the request log) and the finished run's row is still returned; `since=None` reads all histories; a bad token, a 500, an unreachable port, and a non-JSON body each give `error` with the messages above and empty lists; the credential is sent as a bearer header and never in a URL; a redirect is not followed.
- End to end through `TrackingService` with the fake: a first sync fills the record, `set_alias` to a new version then a second sync adds exactly one deployment with the right `from_version`, a third sync with nothing changed adds none (idempotence); a server shut down mid-test keeps the last good data and shows the error.
- Full suite, checks; CI green.

---

### Task 3: The GitHub Actions adapter

**Files:** new `backend/src/mlops_agents/integrations/github/actions.py` (the GitHub call), `backend/src/mlops_agents/tracking/adapters/github_actions.py`, `tracking/adapters/__init__.py` (the `github_actions` entry), `backend/tests/fakes/fake_github.py` (workflow runs: new state `workflow_runs: dict[str, list[dict[str, Any]]]`, `reset` clears it, one route), tests `backend/tests/integrations/test_github_actions.py`, `backend/tests/tracking/test_github_actions_adapter.py`.

**Interfaces:** Consumes the stage 2 GitHub client (`integrations/github/api.py`: `request`, `permission_missing`, `unreadable`) and `TrackingAdapter` (I16). Produces `list_workflow_runs(http, token, repo, *, created_from, limit) -> list[WorkflowRun]` and `GithubActionsAdapter(http)` (`type = "github_actions"`).

**Implementation notes:**
- `list_workflow_runs` calls `GET /repos/{repo}/actions/runs` with `per_page=100`, `created=>=<ISO date>` and `page`, through `request(..., permission="actions: read")` so a 403 or 404 becomes `github_permission_missing`, a 429 the rate-limit error. It stops at `limit` runs. `WorkflowRun` (pydantic, in `actions.py`) holds `id`, `name`, `path`, `status`, `conclusion`, `run_started_at`, `created_at`, `updated_at`, `head_sha`, `html_url`.
- The adapter reads the repository from `GithubActionsSettings`, takes the token from its `credential` argument (the service passes the app's GitHub token; none logged in gives the error "Log in to GitHub to read CI runs"), and keeps a run when the basename of `path` is in `workflows` or `workflows` is empty.
- Window: the first sync (`since is None`) reads the last `GITHUB_FIRST_SYNC_DAYS = 30` days, at most `GITHUB_RUN_CAP = 300` runs; a later sync reads runs created since `since - GITHUB_OVERLAP` (`GITHUB_OVERLAP = 1 day`), so a run that finished since the last sync is updated by the idempotent upsert.
- Mapping to `CiRunRow`: `key` is `str(id)`; `workflow` is the file basename of `path`; `status` and `conclusion` as GitHub gives them (lowercase); `start` is `run_started_at`, else `created_at`; `duration_s` is `updated_at - start` only when `status == "completed"`, else null (the billing-timing endpoint costs a call per run and is not used); `commit` is `head_sha`; `url` is `html_url`.
- Failures become `SyncResult(error=...)` with the `DomainError` message (already user-facing, token-free); the adapter never raises for a GitHub or network failure.

**Tests (stop condition):**
- The new fake route: filters by `created`, pages, honours `required_token` and `simulate_permission_error["actions: read"]`.
- The client function: pages, the `limit`, 401, 429, 403 to `github_permission_missing`, unreadable body.
- The adapter with the fake: mapping of a completed, a failed and an in-progress run (`duration_s` null for the last); the `workflows` filter by basename; first-sync window and later-sync overlap (assert on the `created` parameter in the request log); a run that was in progress and later completed is updated, not duplicated, through `TrackingService`; no GitHub login gives the message; missing permission gives the message.
- Full suite, checks; CI green.

---

### Task 4: The sync loop

**Files:** new `backend/src/mlops_agents/tracking/loop.py` (`TrackingLoop`), `tracking/service.py` (`sync_interval`, `set_sync_interval`), `api/routers/tracking.py` (`GET` and `PUT /api/projects/{id}/tracking/settings`, body `{sync_interval_s: int}`), `api/services.py` (construct, `start`, `stack.push_async_callback(loop.close)`), tests `backend/tests/tracking/test_loop.py`, regenerated API types.

**Interfaces:** Consumes `TrackingService`, `WorkspaceService.list_projects`, task 2 and 3 (the adapters are registered through `default_adapters`). Produces `TrackingLoop(tracking, projects: Callable[[], Awaitable[Iterable[str]]], clock, sleep, tick_s=TICK_S)` with `start()` and `close()`, and the project setting `tracking_sync_interval_s` (default `DEFAULT_SYNC_INTERVAL_S`, minimum `MIN_SYNC_INTERVAL_S`).

**Implementation notes:**
- One app-wide task, started in `build_services` after the stores, in the shape of `WakeupService.start` and `close`: a `_stopped` flag, cancelled and awaited on `close`, a failure in one pass is logged with its traceback and the loop continues after a short sleep, `CancelledError` ends it.
- Every `TICK_S = 30` seconds it lists projects and, for each project that has sources, syncs the sources that are due: `last_sync is None` or `clock() - last_sync >= interval`. A source that failed keeps its old `last_sync`, so it is retried on every tick (at most one attempt per tick; a stated, bounded rate).
- It calls `TrackingService.sync(project_id, source_id)`, so the per-source lock covers a manual sync and a loop sync alike. A project removed while the loop runs, or whose database cannot be opened, is skipped with one log line.
- The interval is a project setting in `project_settings`, as `MessagingSettings` stores its limits (JSON in `value`). A PUT below the minimum is 422.
- The first pass runs at startup, so a project opened after downtime catches up at once. No sync runs in tests unless the test starts the loop: `build_services` takes the loop's start from the app lifespan like the wake-ups, and the API test fixtures already use temp data folders with no sources.

**Tests (stop condition):** with a fake clock and a recording `TrackingService` double: a source never synced is synced on the first pass; a source synced less than its interval ago is skipped, and synced when the clock passes it; changing the interval takes effect on the next pass; a source in error is retried each tick; a project without sources and a removed project cost no call; an exception in one project does not stop the others or the loop; `close` ends the loop during a sleep and during a sync; with the real service and both fakes, a loop started on a temp app fills the record and records `tracking.synced`; the settings route round-trips and refuses an interval below the minimum. Full suite, checks, `npm run gen:api` with no diff; CI green.

---

### Task 5: `TrackingHistory` and `tracking_summary`

**Files:** new `backend/src/mlops_agents/agents/tools/tracking_history.py`, `backend/src/mlops_agents/agents/hooks/tracking_summary.py`, `agents/tools/__init__.py` (one line: the tool, taking `tracking: Tracking | None`), `api/services.py` (registers the hook on the `HookRegistry` and passes `tracking`), tests `backend/tests/agents/tools/test_tracking_history.py`, `backend/tests/agents/test_tracking_summary_hook.py`.

**Interfaces:** Consumes `Tracking.query` (I16), `ContextHook` and `HookRegistry.register` (I18), `AgentTool`, `RiskClass.READ`. Produces the tool `TrackingHistory(what, model?, run_key?, metric?, since?, limit?)` (risk `read`) and the hook named `tracking_summary`.

**Implementation notes:**
- **`TrackingHistory`:** the arguments are `TrackingQuery`'s fields (`extra="forbid"`; `limit` default 50, at most `QUERY_LIMIT_MAX`). It calls `Tracking.query(ctx.project_id, query)` and returns a compact text table (one line per row, key columns first, values cut at 80 characters), then a last line "N of M rows" and, when `truncated`, how to narrow (`since`, `model`, `limit`). No rows gives "No <what> recorded yet" plus, when a source has a `last_error`, that error. It is justified as a tool because the record is in the app's database (spec §7). Bad arguments (`metrics` with no `run_key`) are `ToolFailure` with the service's message.
- **`tracking_summary`** (`name = "tracking_summary"`, `render(ctx) -> str`): reads through `Tracking.query` only. Content: the production model and version (the version holding the alias `production`, else `champion`, else the newest version; says which rule applied), its run's latest metric values (up to 10 metrics), the last 5 runs (key, status, one headline metric if any), the last 5 CI runs (workflow, conclusion, age), and one line per source with a `last_error` or with no sync yet. Capped at `TRACKING_SUMMARY_MAX_CHARS = 3000` (a constant in the hook's module) with the cut noted. No source gives one line: "No tracking source is connected." A failure inside the hook returns one line saying the tracking summary is unavailable and never raises (I18). The hook contains no secret by construction: it reads rows only, never source settings or credentials.
- The tool names and hook name are strings other plans check (`catalog` validates `context_hooks` against `registry.names()`): they are defined once, in these two modules, and imported.

**Tests (stop condition):**
- Tool: each `what` against a service holding fixture rows; the row cap and the "N of M" line; truncation advice; empty record, with and without a source error; `metrics` without `run_key` is a `ToolFailure`; the tool is `read` and the policy allows it at every level.
- Hook: a full record gives the production model by alias, then by `champion`, then by newest (three cases); a record of 200 runs stays under the cap with the cut noted; no source, a source in error, and a service that raises each give their one line; the output contains none of the keystore values placed in the fixtures.
- Registered: `HookRegistry.names()` contains `tracking_summary`; `default_registry` has `TrackingHistory`.
- Full suite, checks; CI green on every job. No early PR: this is the last task.

---

## Interface gaps

- **`credentials/project_secrets.py` has no owner.** I19 says it is the one place that writes credentials (`put`, `move`, `delete`, refs `project/<id>/proposal/<prp>/<slot>` and `project/<id>/tracking/<trk>`), used by tracking and proposals, but no plan's Produces lists it, and tracking task 1 (an early PR) needs `put` and `delete`. Needed: the overview names its producer (contracts task 1 or proposals task 1) before tracking starts; otherwise tracking task 1 creates the file with `put` and `delete` and proposals adds `move`.
- **`tracking.synced` is not in `EventType`.** I13 adds only `turn.stalled` and `turn.resumed`; I15, I16 and I19 name events (`tracking.synced`, `connection.created`, `proposal.created`, ...) that the closed `EventType` literal in `events/contract.py` must list. Needed: contracts adds every stage 3 event; this plan adds `tracking.synced` itself only if it is missing at task 1, which makes `events/contract.py` a file edited by several plans.
- **Table keys.** I16's rows imply, but contracts' migration must state, the unique keys listed in task 1 (notably `run_metrics` on source, run, metric, step and time) and the `last_sync` and `last_error` columns on the sources table.
- **`github_actions` and `auth`.** I16's `auth: none | credential` does not say what a GitHub Actions source uses. This plan fixes it as `none` (the app's GitHub login is the token, passed as the adapter's `credential`). The Onboarding handoff and the proposals plan must not offer a credential slot for a `github_actions` source.
- **Removed aliases.** `DeploymentRow.to_version` is a `str`, so an alias removed in MLflow cannot be recorded as a deployment. Needed only if stage 5 wants "unset" events: `to_version: str | None`.
- **`TrackingQuery.what` has no `models` row shape.** Task 1 defines it (`source_id`, `model`, `latest_version`, `versions`, `aliases`); the pages plan reads it from the generated `schema.d.ts`.
