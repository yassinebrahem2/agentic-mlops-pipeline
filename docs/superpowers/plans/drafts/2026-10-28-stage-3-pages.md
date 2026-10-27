> **Draft.** Missing: the lead's advisor review.

# Stage 3 — Pages Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works. Read the [stage 3 overview](2026-10-28-stage-3-overview.md) first.

**Goal:** The user sees what the agents now know (spec §12): a Models page (tracked models, versions side by side, the production alias, lineage), a Data page (connections, their tables, schema, rows, data version, roles from the handoff) and a Runs page (tracked runs and CI runs in one list, Sync now). All three read the backend routes and refresh live from the project event stream.

**Branch:** `plan/stage-3-pages`.

**Consumes** (generated types in `frontend/src/api/schema.d.ts`, after contracts task 3 and the owning plans merged; names from the overview):
- I16 routes: `GET /api/projects/{id}/tracking/sources`; `POST .../tracking/sync` (optional `source_id`, 202); `GET .../tracking/models` (`Page[ModelSummary]`), `.../versions?model=` (`Page[ModelVersionRow]`), `.../runs` (`Page[RunRow]`), `.../ci-runs` (`Page[CiRunRow]`), `.../deployments` (`Page[DeploymentRow]`), `.../metrics?run_key=&metric=` (`Page[MetricPoint]`); models `TrackingSourceView`, `RunStatus`; event `tracking.synced`.
- I15 routes: `GET /api/projects/{id}/connections`, `GET .../connections/{con}/tables`, `GET .../connections/{con}/tables/{table}`; models `ConnectionView` (with `findings: Findings`), `TableRef`, `TableDetail`, `ColumnInfo`; events `connection.created`, `connection.updated`, `connection.deleted`.
- I20 route: `GET /api/projects/{id}/handoff` (`HandoffView` or `null`, 200; `HandoffPayload`); event `handoff.approved`.
- All five event types are in the backend `EventType` and the frontend `PROJECT_EVENT_TYPES`, written by contracts task 1 ("Shared by contracts"); this plan does not edit `useProjectEvents.ts`.
- Frontend: `PageRoute` and the `import.meta.glob` route table (`app/routes.ts`), `useProjectEvents`, `api` / `unwrap` / `errorText`, `components/ui/*`.

**Produces:** nothing other plans use. The `onboarding` plan's end-to-end test may visit these pages; this plan does not edit `frontend/e2e/flow.spec.ts` (onboarding's file).

**Read first:** spec §7 and §12; `frontend/src/pages/inbox/` (page, route and live-refresh hook), `frontend/src/pages/providers/ModelsTable.tsx` (table, alert and empty-state style), `frontend/src/lib/useProjectEvents.ts`.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Shared hooks and formatters | tracking, connections, data-summary (table detail) and proposals (handoff) routes on `main`; contracts task 3 (types) |
| 2 | Runs page | task 1 |
| 3 | Models page | tasks 1 and 2 (the status badge) |
| 4 | Data page | task 1 |

No task has an early PR: nothing consumes these pages.

**Nav.** Each page's `route.tsx` carries its own `nav` entry and `import.meta.glob` finds it, so `Sidebar.tsx` and `app/routes.ts` need no edit, and an entry exists exactly when its page does. Orders: Agents 0, Inbox 10, Models 20, Data 30, Runs 40, Project settings 100.

---

### Task 1: Shared hooks and formatters

**Files:**
- new `frontend/src/lib/useInvalidateOnEvents.ts`: the one live-refresh helper (the Inbox hook's debounce, extracted).
- `frontend/src/pages/inbox/usePendingApprovals.ts`: `usePendingApprovals`, `useLocalReviews` and `useGithubActivity` (each carries its own copy of the debounce) all switch to the helper (their inline debounce, cleanup effect and event lists go, the event lists becoming the helper's `types` argument; behavior and its tests unchanged, so there is one copy of the rule).
- new `frontend/src/lib/useTracking.ts`, `useConnections.ts`, `useHandoff.ts`: queries and keys.
- new `frontend/src/lib/format.ts`: `formatDuration(seconds)`, `formatTime(iso)`, `shortCommit(sha)`.
- tests beside each.

**Interfaces:**
- Produces `useInvalidateOnEvents(projectId: string | undefined, queryKey: readonly unknown[], types: readonly ProjectEventType[]): void`. On a listed event or `onResync` it invalidates the key after a 150 ms debounce and clears the timer on unmount.
- Produces `trackingKey(projectId, part)`, `part` being `"sources" | "models" | "versions" | "runs" | "ci-runs" | "deployments" | "metrics"`; hooks `useTrackingSources`, `useTrackedModels`, `useModelVersions(projectId, sourceId, model)`, `useTrackedRuns`, `useCiRuns`, `useDeployments(projectId, model?)`, `useRunMetrics(projectId, runKey)`, all refreshed on `tracking.synced` (one invalidation of the `["projects", id, "tracking"]` prefix); `useSyncNow(projectId)`, a mutation on `POST .../tracking/sync` without `source_id` (every source).
- The list hooks return the route's `Page<T>` (`rows`, `total`, `truncated`) unchanged.
- Produces `useConnections(projectId)` (prefix `["projects", id, "connections"]`, refreshed on `connection.created`, `connection.updated`, `connection.deleted`), `useTables(projectId, connectionId)`, `useTableDetail(projectId, connectionId, table, enabled)` (enabled only when a row is expanded), `useHandoff(projectId)` returning `HandoffView | null` (refreshed on `handoff.approved`).
- Types are re-exported from `components["schemas"][...]` only; none is written by hand.

**Implementation notes:**
- Keys all start with `["projects", projectId, ...]` like the existing hooks, so one prefix invalidation clears a project.
- `formatDuration`: `null`/`undefined` gives "–"; under 60 s "12 s"; then "3 min 4 s"; then "1 h 5 min". `formatTime` uses `toLocaleString()` like `ModelsTable` and the Projects page.
- No polling anywhere: the event stream and the Sync button are the only refreshes.

**Tests (stop condition):**
- `useInvalidateOnEvents`: a listed event invalidates once after a burst of three; an unlisted event does nothing; a resync invalidates; unmount cancels a pending invalidation.
- Each hook calls its route with the project id (and its query parameters) and returns the stubbed page; `tracking.synced` refetches a tracking hook; `useHandoff` returns `null` for a null body and surfaces a 500.
- `formatDuration` table cases (`null`, 0, 59, 60, 3665, 7260); `shortCommit` takes the first 7 characters and passes `null` through.
- The existing Inbox tests still pass.
- `npm test`, `npm run lint`, `npm run format:check`, `npm run build`.

---

### Task 2: Runs page

**Files:** new `frontend/src/pages/runs/route.tsx`, `RunsPage.tsx`, `RunsTable.tsx`, `SourceStatus.tsx`, `StatusBadge.tsx`, `mergeRuns.ts`, tests.

**Interfaces:** Consumes `useTrackedRuns`, `useCiRuns`, `useTrackingSources`, `useSyncNow` (task 1). Produces `StatusBadge({ status: RunStatus })` (also used by task 3) and `mergeRuns(runs: Page<RunRow>, ci: Page<CiRunRow>): RunListItem[]`, where `RunListItem = { kind: "run" | "ci"; sourceId; key; name; status: RunStatus; start: string | null; durationSeconds: number | null; detail: string | null; url: string | null }`.

**Implementation notes:**
- Route: path `/projects/:projectId/runs`, `nav: { area: "project", label: "Runs", order: 40, icon: Activity }`. The page renders nothing without a `projectId`, like `InboxPage`.
- One table, newest first by `start` (rows without `start` last): kind (Run or CI icon), name (experiment, or workflow), status badge, started, duration, detail. A tracked run's duration is `end - start` (null while running); a CI run's is `duration_s`. A CI run's detail is its short commit and a link to `url` (`target="_blank" rel="noopener noreferrer"`); a tracked run's is its short `git_commit` and dataset names.
- Statuses are the adapters' normalised `RunStatus` (`running`, `succeeded`, `failed`, `cancelled`, `other`); the page does no vendor mapping. Badge text and variant per value live only in `StatusBadge.tsx`.
- When either page is truncated, the merged list is cut at the later of the two pages' oldest `start` (`mergeRuns` takes the two `truncated` flags), so the tail never implies a period without CI runs or runs where they were simply not loaded.
- A filter (`Tabs`): All / Failed. "Failures" is the spec's word; the Failed filter narrows the same list, with no second query.
- Header: "Sync now" button (`useSyncNow`), disabled while the mutation is pending, showing `errorText` on failure. A 202 only means the sync started; the table refreshes on `tracking.synced`, not on the response.
- When either page has `truncated: true`, a muted line says "Showing the latest N of TOTAL" with the page's own `rows.length` and `total`.
- `SourceStatus`: one line per source: its type, "Last sync <time>" or "Never synced", and, when `last_error` is set, a destructive `Alert` with the error (the last good data stays listed below it; spec §7).
- States: loading, per-query error (`errorText`), no sources ("No tracking source yet. Onboarding creates them when you approve the handoff."), sources but no rows ("Nothing tracked yet. Use Sync now.").

**Tests (stop condition):** with stubbed routes and a `FakeStream`:
- Runs and CI runs merge into one list, newest first, each with the right badge and duration (`end - start`; `duration_s`; running shows "–").
- The Failed filter shows only failed rows of both kinds.
- Sync now posts once with no `source_id`, is disabled while pending, shows a server error; a `tracking.synced` event refetches the rows and the sources.
- A source with `last_error` shows it above the still-listed rows; no source, and no rows, each show their message; a route error shows its text; a truncated page shows the "Showing" line.
- `mergeRuns` units: ordering, null starts last, both duration rules, the cut at the later oldest `start` when one page is truncated and none when neither is.
- Checks as task 1.

---

### Task 3: Models page

**Files:** new `frontend/src/pages/tracked-models/route.tsx`, `TrackedModelsPage.tsx`, `ModelCard.tsx`, `VersionsTable.tsx`, `buildVersionRows.ts`, tests. (The folder is not `models/`, which would sit confusingly beside the providers' `ModelsTable`.)

**Interfaces:** Consumes `useTrackedModels` (`Page[ModelSummary]`), `useModelVersions`, `useTrackedRuns`, `useDeployments`, `useRunMetrics`, `useTrackingSources`, `useHandoff` (task 1), `StatusBadge` (task 2). Produces `buildVersionRows(versions: ModelVersionRow[], runs: RunRow[], metrics: Record<string, MetricPoint[]>): VersionRow[]` with `VersionRow = { version; aliases; created; run: RunRow | null; metrics: Record<string, number> }`, and `metricNames(rows): string[]`.

**Implementation notes:**
- Route: path `/projects/:projectId/models`, `nav: { area: "project", label: "Models", order: 20, icon: Boxes }`.
- One `Card` per `ModelSummary` (keyed by `source_id` and `name`): its name, the production badge showing `production_version` (no badge when null), the count `versions`, then the versions table, newest first. A card requests its versions (`useModelVersions`) when rendered.
- Versions side by side: one row per `ModelVersionRow`; columns are version, aliases (badges), created, then one column per metric name in the union over the model's versions (sorted by name), each cell the metric's value at its highest `step` for that version's run, formatted to 4 significant digits, "–" when absent. Metrics are requested per run with `useRunMetrics(projectId, run_key)` for each version with a `run_key`. No best value is highlighted: whether higher is better is not this page's call. The column of the handoff's primary metric (§9 profile, from `useHandoff`) carries a "primary" badge; with no handoff no column is marked.
- Lineage per version, in a row detail (a `Collapsible` under the row): run (experiment, `StatusBadge`, started, link to the Runs page), git commit (short, full in `title`), training data (`RunRow.datasets`, "Not recorded" when empty), params and tags as a key/value list. A version whose `run_key` is null says "Run not tracked". A version whose run is missing from `useTrackedRuns` says "Run not tracked" when that page is not truncated, and "Run not in the loaded runs" when it is (the run may be older than the page).
- "Alias history": the model's `DeploymentRow`s from `useDeployments(projectId, model)` (newest 10 shown) (alias, from to `to_version`, seen at; a null `to_version` reads "removed"), collapsed by default; a truncated page adds "History truncated".
- A metrics page with `truncated: true` adds "Metrics truncated" to that version's detail, so a cut history is never shown as complete (see Interface gap 1).
- States as task 2: no source; sources but no models ("No models tracked yet"); errors with `errorText`; a source's `last_error` in a warning `Alert` at the top.
- Rows keep one height (the stage 2 rule): details live in the collapsible, not in the cell.

**Tests (stop condition):**
- `buildVersionRows` units: the run join by `run_key` and `source_id`; the highest-step value per metric; a version with no run; `metricNames` union and order.
- The page renders two models, each with versions side by side and the production badge from `production_version`; a metric absent for a version shows "–"; the primary-metric column is badged when the handoff names it.
- Expanding a version shows run, short commit, datasets ("Not recorded" when empty); "Run not tracked" for a null `run_key`; a null `to_version` in the alias history shows "removed".
- A `tracking.synced` event refetches and updates the table; empty and error states.
- Checks as task 1.

---

### Task 4: Data page

**Files:** new `frontend/src/pages/data/route.tsx`, `DataPage.tsx`, `ConnectionCard.tsx`, `TableRow.tsx`, `TableDetailPanel.tsx`, `roles.ts`, tests.

**Interfaces:** Consumes `useConnections`, `useTables`, `useTableDetail`, `useHandoff` (task 1). Produces `roleFor(handoff: HandoffView | null, connectionName: string, table: string): { role: string; idColumn: string | null; timeColumn: string | null } | null` (reads the handoff's data-roles part; exact field names from the generated `HandoffPayload`, the only place that reads them).

**Implementation notes:**
- Route: path `/projects/:projectId/data`, `nav: { area: "project", label: "Data", order: 30, icon: Database }`.
- One `Card` per connection: name, kind, a status badge (`ok`, or `unreachable` with `status_detail` visible), and its tables. It shows `findings` only: one warning line naming `secret_files` when not empty, and one muted line naming `unsupported_files` when not empty. It renders no `settings`: nothing outside `connections/kinds/` knows what a connection is (spec §4).
- Tables list (`useTables`): name, format, number of files, the role badge from `roleFor` ("Not assigned" when the handoff names none; no badge, and one line "Roles appear after you approve the handoff", when `useHandoff` is `null`). Tables of an unreachable connection are not requested.
- Expanding a table opens `TableDetailPanel` (`useTableDetail`, requested on first expand only): row count, data version, the column list (name and type, compact table), the `summary` text in a scrollable `pre` (whitespace preserved, rendered as text), and the role's ID and time columns. A route error shows `errorText` inside the panel; other tables stay usable.
- Footer, always visible: "Quality and drift history arrives with monitoring." (a muted line, exactly this text).
- `connection.created`, `connection.updated` and `connection.deleted` invalidate the connections prefix (cards, tables, details); `handoff.approved` refreshes the roles.
- Empty state: "No data connection yet. Add one in Project settings, or approve Onboarding's connections proposal." with a link to `/projects/:id/settings` (the Project settings route's path).

**Tests (stop condition):**
- Two connections render; the unreachable one shows its detail and requests no tables.
- Tables render with roles from a handoff; "Not assigned" for a table the handoff omits; the "after you approve" line and no badges with a `null` handoff.
- Expanding a table requests the detail once (collapse and re-expand does not refetch), shows row count, data version, columns, summary; a 4xx shows its message in the panel only.
- `findings.secret_files` and `unsupported_files` lines shown; no `settings` value is in the DOM.
- A `connection.created` event adds the card, `connection.deleted` removes it; `handoff.approved` fills in roles.
- The footer line is present in every state.
- `roleFor` units: connection and table match, no match, null handoff.
- Checks as task 1; then, with all tasks in, `npm run build` and `npm run e2e` (it must stay green; no e2e file is edited: onboarding owns `flow.spec.ts`).

---

## Interface gaps

1. **Metrics volume and the list limits.** `metrics?run_key=&metric=` returns a `Page[MetricPoint]` capped by `TrackingQuery.limit` (default 50), so a run with a long history truncates before its highest step is reached; and the overview names no `limit` query parameter on `models`, `versions`, `runs`, `ci-runs`, `deployments`, `metrics`. Needed: a `limit` parameter on each list route (the Runs page wants more than 50); a `run_key` filter on `/runs` (lineage looks runs up by key); a `model` filter on `/deployments` (alias history is per model); a `source_id` on `/versions` (a summary is keyed by `source_id` and name, so two sources with one model name would mix versions); and for metrics either a `latest=true` mode (the highest step per metric) or a guarantee that the cap applies per metric. Task 3 works without these but never presents a cut page as complete: it says "Metrics truncated", "History truncated" or "Run not in the loaded runs".
2. **`HandoffPayload` field names.** The data roles (table to role, ID column, time column) and the primary metric are "exactly the spec's §9 fields" but their names are not listed in I19. Contracts writes them; `roles.ts` and the primary-metric badge read the generated types, so tasks 3 and 4 wait for contracts task 3 (already in their Consumes).
