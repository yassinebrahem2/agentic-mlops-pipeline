> **DRAFT. Do not implement.** Still missing: an advisor review round with no blocking issues. The lead removes this banner when the plan leaves `drafts/`.

# Stage 1 — Frontend Foundation Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. Each task is one PR into `main` (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** What every page plugs into: pages that register themselves, the app shell (sidebar with app and project areas, gear menu), the typed API client generated from the backend's contract, query setup and error text, and sign-in for both run modes.

**Branch:** `plan/stage-1-frontend-foundation`. **Consumes:** I3's contract (`frontend/openapi.json`) for task 2, I4 for task 3. **Produces:** I5 of the stage overview (task 1: page registration; task 2: the API client).

**Read first:** the stage overview (global constraints, "Interfaces between plans" I3 to I5), spec section 6 (UI, Routing, API) and section 7, and `AGENTS.md` (Frontend commands, Code quality).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Page registration and the app shell | merged code |
| 2 | Typed API client, queries and error text | task 1; api-foundation task 1 on `main` |
| 3 | Sign-in for both run modes | task 2; api-foundation task 3 on `main` |

The project switcher in the sidebar lists projects, so it comes with the projects routes: frontend-workspace adds it to the shell's sidebar header.

---

### Task 1: Page registration and the app shell

**Files:**
- Create: `frontend/src/app/routes.ts`, `frontend/src/app/router.tsx`, `frontend/src/app/shell/AppShell.tsx`, `frontend/src/app/shell/Sidebar.tsx`, `frontend/src/app/shell/GearMenu.tsx`, `frontend/src/app/NotFound.tsx`
- Modify: `frontend/src/App.tsx` (renders the router), `frontend/src/main.tsx` if needed, `frontend/eslint.config.js` (`"react-refresh/only-export-components": ["warn", { allowConstantExport: true, allowExportNames: ["route"] }]`: `allowConstantExport` covers only strings and numbers, so without this every page's `route` export warns; eslint-plugin-react-refresh 0.4.20 supports the option)
- Test: `frontend/src/app/routes.test.ts`, `frontend/src/app/shell/AppShell.test.tsx`, `frontend/src/App.test.tsx` (updated)

**Interfaces:**
- Consumes: React Router 7 (`createBrowserRouter`, `RouterProvider`, `Outlet`, `NavLink`, `useParams`), shadcn components already in `src/components/ui/`, `APP_NAME`.
- Produces (I5, first half):
  ```ts
  // src/app/routes.ts
  export type PageRoute = {
    path: string;                  // "/providers", "/projects/:projectId/agents"
    element: React.ReactNode;
    nav?: { area: "app" | "project"; label: string; order: number; icon?: LucideIcon };
  };
  export function collectRoutes(modules: Record<string, { route: PageRoute }>): PageRoute[];
  export const pageRoutes: PageRoute[];   // collectRoutes(import.meta.glob("../pages/**/route.tsx", { eager: true }))
  ```
  A page is a folder `src/pages/<name>/` whose `route.tsx` exports only `route`; the page's components live in other files of that folder. Nothing else is edited to add a page.

**Implementation notes:**
- `collectRoutes` validates: two routes with the same `path` → throw an error naming both files; it sorts nav entries by `area`, then `order`, then `label`.
- The router: one layout route rendering `AppShell` with every page route as a child, plus `*` → `NotFound` ("This page does not exist." and a link home).
- `AppShell`: a sidebar and the page area. The sidebar shows `APP_NAME` at the top (a link to `/`), then the `project` area's entries when the URL is under `/projects/:projectId` (each entry's path with `:projectId` filled from the URL), then the `app` area's entries, then the gear menu at the bottom. The gear menu holds the `app` entries again for small screens and, from task 3, "Sign out". The sidebar collapses into a sheet below the `md` breakpoint (a button opens it).
- `buildRouter(routes, createRouter)` adds a `Welcome` child at `/` ("No pages yet.") only when no registered route has `path: "/"`. It is never a registered route itself, so frontend-workspace registering `/` for Projects replaces it without a duplicate path.
- Only pages that work are registered (spec section 6): no placeholder pages, no menu entries without a page.

**Tests (behaviors to verify):** `collectRoutes` returns every module's route, rejects a duplicate path naming both modules, and orders nav entries; with two fake modules (one `app`, one `project` route) rendered through the real router (`createMemoryRouter` with the same route tree builder): the app entry is in the sidebar at `/`, the project entry appears only under `/projects/prj_1/...` with `prj_1` in its link, an unknown path shows `NotFound`, the button that opens the narrow-screen sidebar shows the entries (jsdom ignores Tailwind breakpoints, so test the button, not a window size); `/` shows "No pages yet." with no module registering `/`, and the fake module's page when one does. Export the route-tree builder (`buildRouter(routes, createRouter)`) so tests use the real tree with a memory router.

**Checks (from `frontend/`):** `npm run lint`, `npm run format:check`, `npm test`, `npm run build`.

---

### Task 2: Typed API client, queries and error text

**Files:**
- Create: `frontend/src/lib/api.ts`, `frontend/src/lib/query.ts`, `frontend/src/lib/api.test.ts`
- Modify: `frontend/package.json` (scripts below), `frontend/.gitignore` (`src/api/schema.d.ts`), `.github/workflows/frontend.yml` (if the pre-scripts below do not already cover CI), `frontend/src/App.tsx` (the query provider)

**Interfaces:**
- Consumes: `frontend/openapi.json` (I3, exported by the backend and committed), the error shape `{code, message, detail}`, the CSRF cookie and header names from I4 (`csrf_token`, `X-CSRF-Token`).
- Produces (I5, second half):
  ```ts
  // src/lib/api.ts
  export type paths = import("../api/schema").paths;
  export function createApiClient<P extends {}>(fetchImpl?: typeof fetch): Client<P>;  // openapi-fetch, baseUrl "", CSRF middleware
  export const api: Client<paths>;                       // createApiClient<paths>()
  export class ApiError extends Error { code: string; status: number; detail: unknown }
  export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T;  // throws ApiError
  export function errorText(error: unknown): string;
  // src/lib/query.ts
  export const queryClient: QueryClient;
  ```

**Implementation notes:**
- Types: `src/api/schema.d.ts` is generated, never committed (`.gitignore`), so it never conflicts. `package.json` gains `"predev"`, `"prebuild"`, `"pretest"` and `"prelint"`, each `npm run gen:api`, so every command and CI regenerate it from the committed `openapi.json`.
- When this task starts, the generated schema may hold no routes yet (api-foundation's first router, `auth`, arrives in its task 3), so nothing here may depend on a specific route. `createApiClient` builds `createClient<P>({ baseUrl: "", fetch: fetchImpl })` with one middleware: on POST, PUT, PATCH and DELETE it reads the `csrf_token` cookie (`document.cookie`) and sets `X-CSRF-Token`.
- `unwrap`: returns `data`; otherwise throws `ApiError` from the body when it has the I3 shape, or `ApiError("http_<status>", "The server answered with an error (HTTP <status>).")` when it does not. A network failure (fetch rejects) becomes `ApiError("offline", "The app's server is not reachable. Check that it is still running.")`.
- `errorText`: `ApiError` → its `message`; anything else → "Something went wrong. Try again." Pages show `errorText(error)` and nothing else from an error.
- `queryClient`: queries retry once except on `ApiError` with status 4xx; a 401 (`not_signed_in`) from any query or mutation (an `onError` on both `QueryCache` and `MutationCache`) invalidates the `["auth"]` query, which brings back the sign-in screen (task 3).

**Tests (behaviors to verify)** (`createApiClient<TestPaths>(fakeFetch)` with a small hand-written `TestPaths` type in the test file; no real server): a POST carries the CSRF header taken from the cookie and a GET does not; `unwrap` throws `ApiError` with the server's `code` and `message`, the generic text for a non-JSON 502, and `offline` when fetch rejects; `errorText` never returns raw response text; a 401 from a query and from a mutation each invalidate `["auth"]`.

**Checks:** as task 1.

---

### Task 3: Sign-in for both run modes

**Files:**
- Create: `frontend/src/app/auth/AuthGate.tsx`, `frontend/src/app/auth/SignIn.tsx`, `frontend/src/app/auth/useAuth.ts`, tests beside them
- Modify: `frontend/src/app/router.tsx` (the gate wraps the shell), `frontend/src/app/shell/GearMenu.tsx` ("Sign out")

**Interfaces:**
- Consumes: task 2's `api`, `unwrap`, `errorText`, `queryClient`; I4's routes (`GET /api/auth/status`, `POST /api/auth/token`, `POST /api/auth/login`, `POST /api/auth/logout`) and the `#token=` link.
- Produces: `useAuth(): { status: AuthStatus | undefined; signOut(): Promise<void> }` (query key `["auth"]`).

**Implementation notes:**
- `AuthGate` loads `["auth"]`. On first load, when `location.hash` holds `token=`, it posts the token (once: the exchange is one promise held at module level, because `StrictMode` runs effects twice in development) to `/api/auth/token` before anything else, then removes the hash with `history.replaceState` (so the token does not stay in the address bar or the history), then refetches status.
- Signed in → the shell. Not signed in, local mode → a page: "Open the app with the link printed in the terminal where it runs." (a wrong or old token shows the server's `bad_token` message above it). Server mode → a password form (label "Password", button "Sign in"); errors shown with `errorText` (wrong password, too many attempts).
- "Sign out" in the gear menu posts logout, clears the query cache, and shows the sign-in screen.
- While status loads, a quiet loading state (no flash of the sign-in page).

**Tests (behaviors to verify)** (custom `fetch`): a `#token=abc` URL posts `{"token": "abc"}` exactly once when rendered inside `<StrictMode>`, removes the hash, and shows the shell when status turns signed in; a rejected token shows the `bad_token` message and the local-mode instructions; server mode shows the password form, a wrong password shows "Wrong password.", the right one shows the shell; "Sign out" posts logout and returns to the sign-in screen; the token never appears in `history` after the exchange (check `window.location.href`).

**Checks:** as task 1. Also by hand with `uv run mlops-agents start --dev` and `npm run dev` (once api-foundation task 4 is on `main`; until then say in the PR that this check waits for it): the printed link signs in and the address bar loses the token.

---

## Not in this plan

- Every page (frontend-workspace, frontend-providers, frontend-chat) and the project switcher (frontend-workspace).
