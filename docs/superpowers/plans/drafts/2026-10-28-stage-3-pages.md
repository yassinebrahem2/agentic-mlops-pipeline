> **Draft.** Missing: the lead's advisor review.

# Stage 3 — Pages Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works. Read the [stage 3 overview](2026-10-28-stage-3-overview.md) first.

**Goal:** The user sees what the agents now know (spec §12): a Models page (tracked models, versions side by side, the production alias, lineage), a Data page (connections, their tables, schema, rows, data version, roles from the handoff) and a Runs page (tracked runs and CI runs in one list, Sync now). All three read the backend routes and refresh live from the project event stream.

**Branch:** `plan/stage-3-pages`.

**Consumes** (generated types in `frontend/src/api/schema.d.ts`, after contracts task 3 and the owning plans merged; names from the overview):
- I16 routes: `GET /api/projects/{id}/tracking/sources`, `POST .../tracking/sync`, `GET .../tracking/{models|runs|ci-runs|deployments}`; models `TrackingSourceView`, `ModelVersionRow`, `RunRow`, `CiRunRow`, `DeploymentRow`, `MetricPoint`; event `tracking.synced`.
- I15 routes: `GET /api/projects/{id}/connections`, `GET .../connections/{con}/tables`, `GET .../connections/{con}/tables/{table}`; models `ConnectionView`, `TableRef`, `TableDetail`, `ColumnInfo`; events `connection.created`, `connection.deleted`.
- I20 route: `GET /api/projects/{id}/handoff` (`HandoffView`, `HandoffPayload`); event `handoff.approved`.
- Frontend: `PageRoute` and the `import.meta.glob` route table (`app/routes.ts`), `useProjectEvents`, `api` / `unwrap` / `errorText`, `components/ui/*`.

**Produces:** nothing other plans use. The `onboarding` plan's end-to-end test may visit these pages; this plan does not edit `frontend/e2e/flow.spec.ts` (onboarding's file).

**Read first:** spec §7 and §12; `frontend/src/pages/inbox/` (page, route and live-refresh hook), `frontend/src/pages/providers/ModelsTable.tsx` (table, alert and empty-state style), `frontend/src/lib/useProjectEvents.ts`.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Shared hooks, formatters and event names | tracking, connections, data-summary (table detail) and proposals (handoff) routes on `main` |
| 2 | Runs page | task 1 |
| 3 | Models page | tasks 1 and 2 (the status badge); **blocked until Interface gap 1 (the metrics route) is decided** |
| 4 | Data page | task 1 |

No task has an early PR: nothing consumes these pages.

**Sidebar.** `app/shell/Sidebar.tsx` and `app/routes.ts` need no edit: a page's `route.tsx` carries its `nav` entry and `import.meta.glob` finds it, so an entry exists exactly when its page does ("the sidebar shows Models, Data and Runs only with their pages"). Orders: Agents 0, Inbox 10, Models 20, Data 30, Runs 40, Project settings 100.

---

### Task 1: Shared hooks, formatters and event names

**Files:**
- `frontend/src/lib/useProjectEvents.ts`: add `"tracking.synced"`, `"connection.created"`, `"connection.deleted"`, `"handoff.approved"` to `PROJECT_EVENT_TYPES` (the SSE stream is subscribed by event name, so an unlisted event never reaches a hook; `satisfies` keeps the names tied to the generated `EventRecord` type). Test in `useProjectEvents.test.ts`.
- new `frontend/src/lib/useInvalidateOnEvents.ts`: the one live-refresh helper (the Inbox hook's debounce, extracted; `usePendingApprovals` uses it too).
- `frontend/src/pages/inbox/usePendingApprovals.ts`: `usePendingApprovals` switches to the helper (its inline debounce and event list go; behavior and its tests unchanged, so there is one copy of the rule).
- new `frontend/src/lib/useTracking.ts`, `useConnections.ts`, `useHandoff.ts`: queries and keys.
- new `frontend/src/lib/format.ts`: `formatDuration(seconds)`, `formatTime(iso)`, `shortCommit(sha)`.
- tests beside each.

**Interfaces:**
- Produces `useInvalidateOnEvents(projectId: string | undefined, queryKey: readonly unknown[], types: readonly ProjectEventType[]): void`. On a listed event or `onResync` it invalidates the key after a 150 ms debounce and clears the timer on unmount.
- Produces `trackingKey(projectId, part)` where `part` is `"sources" | "models" | "runs" | "ci-runs" | "deployments"`; hooks `useTrackingSources`, `useTrackedModels`, `useTrackedRuns`, `useCiRuns`, `useDeployments`, each `(projectId: string | undefined)`, all refreshed on `tracking.synced`; `useSyncNow(projectId)`, a mutation on `POST .../tracking/sync` (no body: every source).
- Produces `useConnections(projectId)` (refreshed on `connection.created`, `connection.deleted`), `useTables(projectId, connectionId)`, `useTableDetail(projectId, connectionId, table, enabled)` (enabled only when a row is expanded), `useHandoff(projectId)` (refreshed on `handoff.approved`).
- Types are re-exported from `components["schemas"][...]` only; none is written by hand.

**Implementation notes:**
- Keys all start with `["projects", projectId, ...]` like the existing hooks, so one prefix invalidation clears a project.
- `useHandoff` returns `HandoffView | null`: a project without an approved handoff yields `null`, whether the route answers 404 or a null body (an `ApiError` with `status === 404` becomes `null`; any other error propagates).
- `useTableDetail` has `retry: false` semantics through the app's default (4xx are not retried); its error text is shown by the Data page.
- `formatDuration`: `null`/`undefined` gives "–"; under 60 s "12 s"; then "3 min 4 s"; then "1 h 5 min". `formatTime` uses `toLocaleString()` like `ModelsTable` and the Projects page.
- No polling anywhere: the event stream and the Sync button are the only refreshes.

**Tests (stop condition):**
- `useProjectEvents.test.ts`: the new names are subscribed (a `FakeStream` event of each reaches the callback).
- `useInvalidateOnEvents`: a listed event invalidates once after a burst of three; an unlisted event does nothing; a resync invalidates; unmount cancels a pending invalidation.
- Each hook calls its route with the project id and returns the stubbed rows; `useHandoff` maps a 404 to `null` and surfaces a 500.
- `formatDuration` table cases (`null`, 0, 59, 60, 3665, 7260); `shortCommit` takes the first 7 characters and passes `null` through.
- `npm test`, `npm run lint`, `npm run format:check`, `npm run build`.

---

### Task 2: Runs page

**Files:** new `frontend/src/pages/runs/route.tsx`, `RunsPage.tsx`, `RunsTable.tsx`, `SourceStatus.tsx`, `StatusBadge.tsx`, `mergeRuns.ts`, tests.

**Interfaces:** Consumes `useTrackedRuns`, `useCiRuns`, `useTrackingSources`, `useSyncNow` (task 1). Produces `StatusBadge({ status: string, conclusion?: string | null })` (also used by task 3) and `mergeRuns(runs: RunRow[], ci: CiRunRow[]): RunListItem[]`, where `RunListItem = { kind: "run" | "ci"; sourceId; key; name; status: "running" | "succeeded" | "failed" | "other"; start: string | null; durationSeconds: number | null; detail: string | null; url: string | null }`.

**Implementation notes:**
- Route: path `/projects/:projectId/runs`, `nav: { area: "project", label: "Runs", order: 40, icon: Activity }`. The page renders nothing without a `projectId`, like `InboxPage`.
- One table, newest first by `start` (rows without `start` last): kind (Run or CI icon), name (experiment, or workflow), status badge, started, duration, detail. A tracked run's duration is `end - start` (null while running); a CI run's is `duration_s`. A CI run's detail is its short commit and a link to `url` (`target="_blank" rel="noopener noreferrer"`); a tracked run's is its short git commit (`git_commit`) and dataset names.
- Status mapping lives only in `mergeRuns.ts`: tracked runs `FINISHED` is succeeded, `FAILED` and `KILLED` are failed, `RUNNING` is running; CI `conclusion` `success` is succeeded, `failure`, `timed_out`, `cancelled`, `startup_failure` are failed, no conclusion with status `in_progress` or `queued` is running; anything else is `other` and shows the raw text. The two adapters' vocabularies are confirmed against the fake MLflow server and the GitHub adapter's tests of the tracking plan before the tests are written.
- A filter row: All / Failed (a `Tabs`, the existing component). "Failures" is the spec's word; the Failed filter is the same list narrowed, not a second query.
- Header: "Sync now" button (`useSyncNow`), disabled while the mutation is pending, showing `errorText` on failure. The table refreshes on `tracking.synced`, not on the response.
- `SourceStatus`: one line per source: its type, "Last sync <time>" or "Never synced", and, when `last_error` is set, a destructive `Alert` with the error (the last good data stays listed below it; spec §7).
- States: loading, per-query error (`errorText`), no sources ("No tracking source yet. Onboarding creates them when you approve the handoff."), sources but no rows ("Nothing tracked yet. Use Sync now.").

**Tests (stop condition):** with stubbed routes and a `FakeStream`:
- Runs and CI runs merge into one list, newest first, each with the right status badge and duration (`end - start`; `duration_s`; running shows "–").
- The Failed filter shows only failed rows of both kinds.
- Sync now posts once, is disabled while pending, shows a server error; a `tracking.synced` event refetches the rows and the sources.
- A source with `last_error` shows it above the still-listed rows; no source, and no rows, each show their message; a route error shows its text.
- `mergeRuns` unit table for every status mapping and the unknown fallback.
- Checks as task 1.

---

### Task 3: Models page

**Blocked until Interface gap 1 is decided:** the versions-side-by-side table needs per-run metrics, and no route serves them. Do not start this task before the lead's answer; stop and say so in the plan issue.

**Files:** new `frontend/src/pages/tracked-models/route.tsx`, `TrackedModelsPage.tsx`, `ModelCard.tsx`, `VersionsTable.tsx`, `groupVersions.ts`, tests. (The folder is not `models/`, which would sit confusingly beside the providers' `ModelsTable`.)

**Interfaces:** Consumes `useTrackedModels` (rows of `ModelVersionRow`), `useTrackedRuns`, `useDeployments`, `useTrackingSources` (task 1), `StatusBadge` (task 2), and the metrics route of Interface gap 1 (a `useRunMetrics(projectId)` hook is added to `useTracking.ts` by this task once the route's shape is known). Produces `groupVersions(versions, runs, metrics, deployments): ModelGroup[]`, with `ModelGroup = { model: string; versions: VersionRow[]; metricNames: string[]; productionVersion: string | null; history: DeploymentRow[] }` and `VersionRow = { version; aliases; created; run: RunRow | null; metrics: Record<string, number> }`.

**Implementation notes:**
- Route: path `/projects/:projectId/models`, `nav: { area: "project", label: "Models", order: 20, icon: Boxes }`.
- One `Card` per model: its name, the production badge with the version that holds the alias `production` (one constant, `PRODUCTION_ALIAS`, in `groupVersions.ts`), then the versions table, newest first.
- Versions side by side: one row per version; columns are version, aliases (badges), created, then one column per metric name in the union over the model's versions (sorted by name), each cell the metric's value at its highest step for that version's run (`MetricPoint` with the largest `step`), formatted to 4 significant digits, "–" when absent. No best value is highlighted: whether higher is better is not this page's call. The column of the handoff's primary metric (§9 profile; `useHandoff`, field name per gap 4) carries a "primary" badge; with no handoff no column is marked.
- Lineage per version, in a row detail (a `Collapsible` under the row): run (experiment, `StatusBadge`, started, link to the Runs page), git commit (short, full in `title`), training data (`RunRow.datasets`, "Not recorded" when empty), params and tags in a key/value list. A version whose `run_key` is null or whose run is not in the record says "Run not tracked" (no error).
- "Alias history": the model's last 10 `DeploymentRow`s (alias, from → to version, seen at), collapsed by default.
- States as task 2: no source; sources but no models ("No models tracked yet"); errors with `errorText`; `last_error` of a source in a warning `Alert` at the top.
- Rows in a table keep one height (the stage 2 rule): details live in the collapsible, not in the cell.

**Tests (stop condition):**
- `groupVersions` units: grouping by model and sort order; the metric union and highest-step choice; the production version found, and `null` without it; a version with no run; deployments filtered to their model.
- The page renders two models, each with versions side by side and the production badge on the right version; a metric absent for a version shows "–".
- Expanding a version shows run, short commit, datasets ("Not recorded" when empty); "Run not tracked" for a null `run_key`.
- A `tracking.synced` event refetches and updates the table; empty and error states.
- Checks as task 1.

---

### Task 4: Data page

**Files:** new `frontend/src/pages/data/route.tsx`, `DataPage.tsx`, `ConnectionCard.tsx`, `TableRow.tsx`, `TableDetailPanel.tsx`, `roles.ts`, tests.

**Interfaces:** Consumes `useConnections`, `useTables`, `useTableDetail`, `useHandoff` (task 1). Produces `roleFor(handoff: HandoffView | null, connectionName: string, table: string): { role: string; idColumn: string | null; timeColumn: string | null } | null` (the handoff's data-roles part, exact field names read from the generated `HandoffPayload`; the only place that reads them).

**Implementation notes:**
- Route: path `/projects/:projectId/data`, `nav: { area: "project", label: "Data", order: 30, icon: Database }`.
- One `Card` per connection: name, kind, a status badge (`ok`, or `unreachable` with `status_detail` visible), then its tables. A connection card shows only name, kind, status and, when `secret_files` is not empty, one warning line listing those file names (the denylist warning, read-only). It renders no `settings`: nothing outside `connections/kinds/` knows a connection is a folder (spec §4).
- Tables list (`useTables`): name, format, number of files, the role badge from `roleFor` ("Not assigned" when the handoff names none; no badge at all, and one line "Roles appear after you approve the handoff", when `useHandoff` is `null`). Tables of an unreachable connection are not requested.
- Expanding a table opens `TableDetailPanel` (`useTableDetail`, requested on first expand only): row count, data version, the column list (name and type, as a compact table), the data-summary `summary` text in a scrollable `pre` (whitespace preserved, never HTML-injected), and the role's ID and time columns. A route error (the data-summary plan answers 4xx for an unreadable table) shows `errorText` inside the panel; other tables stay usable.
- Footer, always visible: "Quality and drift history arrives with monitoring." (a muted line, exactly this text).
- A connection created or deleted (`connection.created`, `connection.deleted`) updates the list, and so do its tables: `useInvalidateOnEvents` invalidates the `["projects", id, "connections"]` prefix, which includes tables and details; `handoff.approved` refreshes the roles.
- Empty state: "No data connection yet. Add one in Project settings, or approve Onboarding's connections proposal." with a link to `/projects/:id/settings`.

**Tests (stop condition):**
- Two connections render; the unreachable one shows its detail and requests no tables.
- Tables render with roles from a handoff; "Not assigned" for a table the handoff omits; the "after you approve" line and no badges with a `null` handoff.
- Expanding a table requests the detail once (collapse and re-expand does not refetch), shows row count, data version, columns, summary; a 4xx shows its message in the panel only.
- `secret_files` warning shown; no `settings` value is in the DOM.
- A `connection.created` event adds the card, `connection.deleted` removes it; `handoff.approved` fills in roles.
- The footer line is present in every state.
- `roleFor` units: connection/table match, no match, null handoff.
- Checks as task 1; then, with all tasks in, `npm run build` and `npm run e2e` (it must stay green; no e2e file is edited: the onboarding plan's end-to-end test owns `flow.spec.ts`).

---

## Interface gaps

1. **Metrics for the Models page.** I16 defines `MetricPoint` and `TrackingQuery(what="metrics")` but the routes list `tracking/{models|runs|ci-runs|deployments}` only. Task 3 needs per-run metrics. Needed: `GET /api/projects/{id}/tracking/metrics`, optionally filterable by `run_key` list or `model`, returning `MetricPoint` rows (or `models` rows embedding the latest value per metric per version). Without it the "versions side by side" requirement cannot be met.
2. **Response shapes and query parameters of the four tracking list routes** are not named: whether they return `TrackingResult` (`rows`, `total`, `truncated`) or bare lists, which filters they accept (`model`, `since`, `limit`) and the default `limit` (50 in `TrackingQuery`). The pages must show "truncated" honestly and merge runs and CI runs, so the response model and a limit above 50 for the Runs page are needed.
3. **`POST /tracking/sync`:** whether it returns when the sync is finished or at once, and whether it takes an optional `source_id` (the `Tracking.sync` protocol does). The plan assumes no body and does not depend on the response.
4. **`GET /handoff` with no approved handoff:** 404 or a null body is unspecified (the plan handles both). The field names of `HandoffPayload`'s data roles (table to role, ID column, time column) and of its primary metric are "exactly §9's fields" but are not listed; `roles.ts` reads them from the generated types once contracts writes them.
5. **Event names in the SSE subscription.** `PROJECT_EVENT_TYPES` in `frontend/src/lib/useProjectEvents.ts` is not in the overview's shared-files list; this plan adds four names there (task 1). Contracts must add `tracking.synced`, `connection.created`, `connection.deleted`, `handoff.approved` to the `EventRecord` type enum, or `satisfies` fails.
6. **Status vocabularies.** `RunRow.status` and `CiRunRow.status`/`conclusion` are free strings, so the frontend (`mergeRuns.ts`) would hold MLflow's and GitHub's vocabularies, which the adapters also know: two copies of one rule. I16 needs a normalized status (for example `state: "running" | "succeeded" | "failed" | "other"` beside the raw text), or the lead accepts the frontend owning the vendor mapping. Task 2 keeps the mapping in `mergeRuns.ts` meanwhile; the tracking plan's fakes must emit a finished, a failed and a running value for each source type.
7. **Overview fix.** The overview's shared-files line says `Sidebar.tsx` and the route table gain one line per page. They need no edit: each page's `route.tsx` carries its `nav` entry and the glob finds it. The line should say that pages add only their `route.tsx` files.
