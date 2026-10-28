> **Draft.** Missing: the lead's advisor review.

# Stage 3 — Pages Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works. Read the [stage 3 overview](2026-10-28-stage-3-overview.md) first.

**Goal:** The user sees what the agents now know and manages where tracking data comes from (spec §12): a Models page (tracked models, versions side by side, the production alias, lineage), a Data page (connections, their tables, schema, rows, data version, roles from the handoff) and a Runs page (tracked runs and CI runs in one list, Sync now); and, in project settings, the tracking sources section (list, add, credential, last sync and error). All read the backend routes and refresh live from the project event stream.

**Branch:** `plan/stage-3-pages`.

**Consumes** (generated types in `frontend/src/api/schema.d.ts`, regenerated with `npm run gen:api` once contracts task 2 and the owning plans' routes are on `main`; names from the overview). **Starts** after contracts task 2 (the `HandoffPayload` and `ConnectionsPayload` models, and I20's types) and those routes are on `main`. Note the table-detail route (`GET .../connections/{con}/tables/{table}`) arrives with data-summary's plan PR, not an early PR, so the Data page's table expansion (task 4) can start only once that PR has merged; tasks 1 to 3 and 5 do not need it:
- I16 routes: `GET /api/projects/{id}/tracking/sources`; `POST .../tracking/sync` (optional `source_id`, 202); `GET/POST .../tracking/sources` (`POST` body: `type`, `settings`, `auth`); `PUT .../tracking/sources/{trk}/credential` (keystore only); `GET .../tracking/models` (`Page[ModelSummary]`), `.../versions` (`model`, `source_id`; `Page[ModelVersionRow]`), `.../runs` (`run_key`; `Page[RunRow]`), `.../ci-runs` (`Page[CiRunRow]`), `.../deployments` (`model`, `source_id`; `Page[DeploymentRow]`), `.../metrics` (`run_key`, `metric`, `latest`; `Page[MetricPoint]`); every list route takes `limit` (default 50, max 500) and `offset`; models `TrackingSourceView`, `RunStatus`; event `tracking.synced`. The `/evaluations` route is stage 5's and unused here. Behaviors relied on, owned by the tracking plan: `latest=true` returns the highest-`step` point per metric of the run; `/versions` is ordered newest first; a source's `settings` are `{url}` for `mlflow` and `{repository, workflows}` for `github_actions` (`MlflowSettings` and `GithubActionsSettings` in `tracking/contract.py`, `extra="forbid"`, written by contracts task 1; the form builds exactly these keys from the generated types); the credential route answers 422 for a source with `auth="none"`.
- I15 routes: `GET /api/projects/{id}/connections`, `GET .../connections/{con}/tables`, `GET .../connections/{con}/tables/{table}`; models `ConnectionView` (with `findings: Findings`), `TableRef`, `TableDetail`, `ColumnInfo`; events `connection.created`, `connection.updated`, `connection.deleted`.
- I20 route: `GET /api/projects/{id}/handoff` (`HandoffView` or `null`, 200); I19 `HandoffPayload`, of which the pages read `data_roles: list[DataRole]` (`connection`, `table`, `role`, `id_column`, `time_column`) and `models: list[ModelSpec]` (`name`, `profile.primary_metric`, `predictions` and `labels` as `TableUse | None`); event `handoff.approved`. A project has several models and several data roles, so both pages work on lists.
- All five event types are in the backend `EventType` and the frontend `PROJECT_EVENT_TYPES`, written by contracts task 1 ("Shared by contracts"; contracts task 2 builds on it); this plan does not edit `useProjectEvents.ts`.
- Frontend: `PageRoute` and the `import.meta.glob` route table (`app/routes.ts`), `useProjectEvents`, `api` / `unwrap` / `errorText`, `components/ui/*`.

**Produces:** nothing other plans use (the proposals plan's cards say where to set a tracking credential; this section is that place). The `onboarding` plan's end-to-end test may visit these pages; this plan does not edit `frontend/e2e/flow.spec.ts` (onboarding's file).

**Read first:** spec §7 and §12; `frontend/src/pages/inbox/` (page, route and live-refresh hook), `frontend/src/pages/providers/ModelsTable.tsx` (table, alert and empty-state style), `frontend/src/lib/useProjectEvents.ts`.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Shared hooks and formatters | tracking, connections and proposals (handoff) routes on `main`; contracts task 2 (payload types) |
| 2 | Runs page | task 1 |
| 3 | Models page | tasks 1 and 2 (the status badge) |
| 4 | Data page | task 1; connections task 1 (`ConnectionFindings`); data-summary's plan PR merged (the table-detail route) |
| 5 | Tracking sources section in project settings | task 1 (hooks); the tracking sources and credential routes |

No task has an early PR: nothing consumes these pages.

**Nav.** The settings section (task 5) is not a page: `ProjectSettingsPage.tsx` gains one import and one line for it (overview, shared files). Each page's `route.tsx` carries its own `nav` entry and `import.meta.glob` finds it, so `Sidebar.tsx` and `app/routes.ts` need no edit, and an entry exists exactly when its page does. Orders: Agents 0, Inbox 10, Models 20, Data 30, Runs 40, Project settings 100.

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
- Produces `RUNS_PAGE_LIMIT = 200` and `VERSIONS_PAGE_LIMIT = 100` (the pages' `limit` values, within the route max of 500), exported from `useTracking.ts`.
- Produces `trackingKey(projectId, part)`, `part` being `"sources" | "models" | "versions" | "runs" | "ci-runs" | "deployments" | "metrics"`; hooks `useTrackingSources`, `useTrackedModels`, `useModelVersions(projectId, sourceId, model)`, `useTrackedRuns(projectId)` and `useRunByKey(projectId, runKey, enabled)` (the `/runs?run_key=` lookup), `useCiRuns`, `useDeployments(projectId, sourceId, model)`, `useRunMetrics(projectId, runKey)` (always `latest=true`: the highest-step value per metric), all refreshed on `tracking.synced` (one invalidation of the `["projects", id, "tracking"]` prefix); `useSyncNow(projectId)`, a mutation on `POST .../tracking/sync` without `source_id` (every source); `useCreateSource(projectId)` (`POST .../tracking/sources`, invalidates the sources key) and `useSetSourceCredential(projectId)` (`PUT .../tracking/sources/{trk}/credential`, invalidates the sources key; its result and error never carry the value back).
- The list hooks return the route's `Page<T>` (`rows`, `total`, `truncated`) unchanged.
- Produces `useConnections(projectId)` (prefix `["projects", id, "connections"]`, refreshed on `connection.created`, `connection.updated`, `connection.deleted`), `useTables(projectId, connectionId)`, `useHandoff(projectId)` returning `HandoffView | null` (refreshed on `handoff.approved`).
- Types are re-exported from `components["schemas"][...]` only; none is written by hand.

**Implementation notes:**
- Keys all start with `["projects", projectId, ...]` like the existing hooks, so one prefix invalidation clears a project.
- `formatDuration`: `null`/`undefined` gives "–"; under 60 s "12 s"; then "3 min 4 s"; then "1 h 5 min". `formatTime` uses `toLocaleString()` like `ModelsTable` and the Projects page.
- No polling anywhere: the event stream and the Sync button are the only refreshes.

**Tests (stop condition):**
- `useInvalidateOnEvents`: a listed event invalidates once after a burst of three; an unlisted event does nothing; a resync invalidates; unmount cancels a pending invalidation.
- `useCreateSource` posts `type`, `settings`, `auth` and refreshes the sources; `useSetSourceCredential` puts the value to the credential route only, and the value appears in no query key, cache entry or error text.
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

**Interfaces:** Consumes `useTrackedModels` (`Page[ModelSummary]`), `useModelVersions`, `useRunByKey`, `useDeployments`, `useRunMetrics`, `useTrackingSources`, `useHandoff` (task 1), `StatusBadge` (task 2). Produces `buildVersionRows(versions: ModelVersionRow[], metrics: Record<string, MetricPoint[]>): VersionRow[]` (`metrics` keyed by `run_key`) with `VersionRow = { version; aliases; created; runKey: string | null; metrics: Record<string, number> }`, `metricNames(rows): string[]`, and `primaryMetric(handoff: HandoffView | null, modelName: string): string | null` (the `ModelSpec` whose `name` equals the model's name; its `profile.primary_metric`).

**Implementation notes:**
- Route: path `/projects/:projectId/models`, `nav: { area: "project", label: "Models", order: 20, icon: Boxes }`.
- One `Card` per `ModelSummary` (keyed by `source_id` and `name`): its name, the production badge showing `production_version` (no badge when null), the count `versions`, then the versions table, newest first. A card requests its versions (`useModelVersions`) when rendered.
- Versions side by side: one row per `ModelVersionRow`; columns are version, aliases (badges), created, then one column per metric name in the union over the model's versions (sorted by name), each cell the run's `latest` value of that metric (the route's `latest=true`; the page does no step logic), formatted to 4 significant digits, "–" when absent. Metrics are requested per run with `useRunMetrics(projectId, run_key)` for each version with a `run_key`. No best value is highlighted: whether higher is better is not this page's call. The column named by `primaryMetric(handoff, model.name)` carries a "primary" badge; with no handoff, or no `ModelSpec` of that name, no column is marked (each model decides for itself).
- Lineage per version, in a row detail (a `Collapsible` under the row): run (experiment, `StatusBadge`, started, link to the Runs page), git commit (short, full in `title`), training data (`RunRow.datasets`, "Not recorded" when empty), params and tags as a key/value list. The run comes from `useRunByKey`, requested when the row is expanded, so its age never matters. A version whose `run_key` is null, or whose lookup returns no row, says "Run not tracked" (no error).
- "Alias history": the model's `DeploymentRow`s from `useDeployments(projectId, source_id, model)` (newest 10 shown) (alias, from to `to_version`, seen at; a null `to_version` reads "removed"), collapsed by default; a truncated page adds "History truncated".
- A versions page with `truncated: true` adds "Showing the latest N of TOTAL versions" under the table; a deployments page with `truncated: true` adds "History truncated". A cut list is never shown as complete.
- States as task 2: no source; sources but no models ("No models tracked yet"); errors with `errorText`; a source's `last_error` in a warning `Alert` at the top.
- Rows keep one height (the stage 2 rule): details live in the collapsible, not in the cell.

**Tests (stop condition):**
- `buildVersionRows` units: the metric lookup by `run_key`; a version with no `run_key`; `metricNames` union and order; `primaryMetric` with two `ModelSpec`s (right one picked), no handoff, no matching name.
- The page renders two models, each with versions side by side and the production badge from `production_version`; a metric absent for a version shows "–"; the primary-metric column is badged per model from its own `ModelSpec`.
- Expanding a version shows run, short commit, datasets ("Not recorded" when empty); "Run not tracked" for a null `run_key` and for an empty `/runs?run_key=` answer; a null `to_version` in the alias history shows "removed".
- A `tracking.synced` event refetches and updates the table; empty and error states.
- Checks as task 1.

---

### Task 4: Data page

**Files:** new `frontend/src/pages/data/route.tsx`, `DataPage.tsx`, `ConnectionCard.tsx`, `TableRow.tsx`, `TableDetailPanel.tsx`, `roles.ts`, `frontend/src/lib/useConnections.ts` (adds `useTableDetail`), tests.

**Interfaces:** Consumes `useConnections`, `useTables`, `useHandoff` (task 1). Produces `useTableDetail(projectId, connectionId, table, enabled)` in `frontend/src/lib/useConnections.ts` (enabled only when a row is expanded; it is added by this task because the route's types exist only once data-summary's PR has merged). Also consumes `ConnectionFindings` (connections task 1; imported, never copied). Produces `roleFor(handoff: HandoffView | null, connectionName: string, table: string): DataRole | null` (the first entry of `payload.data_roles` with that `connection` and `table`) and `modelsUsing(handoff, connectionName, table): { model: string; as: "predictions" | "labels" }[]` (the `ModelSpec`s whose `predictions` or `labels` `TableUse` names the table). These two are the only readers of the handoff on this page.

**Implementation notes:**
- Route: path `/projects/:projectId/data`, `nav: { area: "project", label: "Data", order: 30, icon: Database }`.
- One `Card` per connection: name, kind, a status badge (`ok`, or `unreachable` with `status_detail` visible), and its tables. Its findings are shown only through connections' `ConnectionFindings` component (connections task 1; `{ findings }` from the `ConnectionView`): this page writes no secret-file or unsupported-file line and no reason text of its own. It renders no `settings`: nothing outside `connections/kinds/` knows what a connection is (spec §4).
- Tables list (`useTables`): name, format, number of files, the role badge from `roleFor` (`role`: training, serving_inputs, predictions, labels or other; "Not assigned" when the handoff names none; no badge, and one line "Roles appear after you approve the handoff", when `useHandoff` is `null`). Tables of an unreachable connection are not requested.
- Expanding a table opens `TableDetailPanel` (`useTableDetail`, requested on first expand only): row count, data version, the column list (name and type, compact table), the `summary` text in a scrollable `pre` (whitespace preserved, rendered as text), the role's `id_column` and `time_column`, and the models using the table (`modelsUsing`, e.g. "Predictions of model-a"). A route error shows `errorText` inside the panel; other tables stay usable.
- Footer, always visible: "Quality and drift history arrives with monitoring." (a muted line, exactly this text).
- `connection.created`, `connection.updated` and `connection.deleted` invalidate the connections prefix (cards, tables, details); `handoff.approved` refreshes the roles.
- Empty state: "No data connection yet. Add one in Project settings, or approve Onboarding's connections proposal." with a link to `/projects/:id/settings` (the Project settings route's path).

**Tests (stop condition):**
- Two connections render; the unreachable one shows its detail and requests no tables.
- Tables render with roles from a handoff holding several roles and two models; "Not assigned" for a table the handoff omits; the "after you approve" line and no badges with a `null` handoff.
- Expanding a table requests the detail once (collapse and re-expand does not refetch), shows row count, data version, columns, summary; a 4xx shows its message in the panel only.
- A connection with secret-like and unsupported files renders them through `ConnectionFindings` (assert with the real component and a finding's backend `reason`); a clean connection shows nothing; no `settings` value is in the DOM.
- A `connection.created` event adds the card, `connection.deleted` removes it; `handoff.approved` fills in roles.
- The footer line is present in every state.
- `roleFor` units: connection and table match, one table name under two connections, no match, null handoff; `modelsUsing` with two models.
- Checks as task 1; then, with all tasks in, `npm run build` and `npm run e2e` (it must stay green; no e2e file is edited: onboarding owns `flow.spec.ts`).

---

### Task 5: Tracking sources section in project settings

**Files:** new `frontend/src/pages/project-settings/TrackingSourcesSection.tsx`, `AddTrackingSourceDialog.tsx`, `SourceCredentialForm.tsx`, tests beside them; `frontend/src/pages/project-settings/ProjectSettingsPage.tsx` (one import and one line, shared with the embedders and connections plans).

**Interfaces:** Consumes `useTrackingSources`, `useCreateSource`, `useSetSourceCredential` (task 1), `TrackingSourceView` and `SourceType` (generated), `formatTime`. Produces nothing.

**Implementation notes:**
- One list, one row per source: type label (MLflow, GitHub Actions), its target (`settings.url` for `mlflow`; `settings.repository` plus the workflow names, or "all workflows", for `github_actions`), the auth state ("No authentication", "Credential set", or "Credential missing" for `auth="credential"` with `credential_set` false), "Last sync <time>" or "Never synced", and `last_error` in a destructive `Alert` when set. The list refreshes on `tracking.synced` (task 1's prefix invalidation). No route removes or edits a source, so the section offers neither.
- Add: a dialog with a type `Select`.
  - `mlflow`: URL, and a switch "Needs a credential" (sets `auth`).
  - `github_actions`: repository (`owner/name`) and workflow file names, comma separated, optional; `auth` is `none` and says it uses the app's GitHub login.
  - The form builds exactly the settings keys of the tracking plan's models. The server's 422 message is shown as given (`errorText`, `fieldErrors` for a field), never re-validated here beyond non-empty.
- Credential: for an `auth="credential"` source, `SourceCredentialForm` has one `type="password"` input (`autoComplete="off"`) and Set. The value is held only in the input's state, sent in the `PUT`, and cleared on success or failure; it is never put in a query key, a toast, a log or the DOM after saving. Setting it again replaces it. After the add dialog creates a credential source it sends the credential in a second call; if that call fails, the source stays listed as "Credential missing" with the form open on its row, with the server's error shown (no rollback, no hidden retry).
- Warnings about an open tracker belong to the backend (the proposal kind's `warnings`); this section adds no sentence of its own about authentication.
- States: loading, error (`errorText`), and no sources ("No tracking source yet. Onboarding adds them when you approve the handoff, or add one here.").

**Tests (stop condition):** with stubbed routes:
- Sources render with target, auth state, last sync and error; the three auth states; empty state; route error.
- Adding an `mlflow` source with a credential posts `{type, settings: {url}, auth: "credential"}` then puts the credential; a `github_actions` source posts `{repository, workflows}` with `auth: "none"` and shows no credential form; a 422 shows its message and keeps the dialog open.
- A failing credential put leaves the source listed as "Credential missing" with the error shown.
- The credential value is absent from every recorded URL and from the DOM after saving, and the input is cleared; the credential input is `type="password"`.
- A `tracking.synced` event refreshes last sync.
- Checks as task 1, and `ProjectSettingsPage.test.tsx` still passes.

---

