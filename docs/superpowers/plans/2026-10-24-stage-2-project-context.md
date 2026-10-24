# Stage 2 — Project Context Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** Opening an app page (AI providers, Integrations, App settings) from inside a project keeps that project selected. The project switcher in the sidebar header fits and reads well (`docs/roadmap.md`, Stage 2).

**Branch:** `plan/stage-2-project-context`. **Consumes:** merged code. **Produces:** nothing other plans use.

**Read first:** the stage 2 overview, and the stage 1 spec §6 (App level and Project level).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | App pages keep the current project | merged code |
| 2 | The sidebar header | task 1 |

---

### Task 1: App pages keep the current project

**Files:** new `frontend/src/lib/useCurrentProject.ts` and `frontend/src/lib/useProjects.ts` with tests, `frontend/src/app/shell/Sidebar.tsx`, `frontend/src/app/shell/ProjectSwitcher.tsx`, `frontend/src/pages/projects/ProjectsPage.tsx`, and the one `invalidateQueries` line in each of `lib/useOpenProject.ts`, `pages/add-project/LocalFolderTab.tsx`, `pages/add-project/GitHubTab.tsx`, `pages/project-settings/RemoveProjectDialog.tsx` and `pages/project-settings/RepositorySection.tsx`, with their tests.

**Interfaces:** Consumes merged code (`GET /api/projects`, whose items carry `last_opened_at`; `useProject`). Produces none.

**Cause:** the current project is read only from the URL (`useParams().projectId`). App routes have no `:projectId`, so on them the Project group disappears and the switcher reads "Projects".

**Implementation notes:**
- `useCurrentProject()` is the one source of the current project:
  - inside a project route, the project in the URL;
  - on an app route, the last project route visited in this page load (held in a small module store the hook updates when the URL has a project). Reloading or deep-linking into a project counts, although it does not call `/open`.
  - on a fresh load straight onto an app route, the project with the latest `last_opened_at`;
  - no project when none was ever opened.
- `useProjects()` is the one projects query, and `projectsKey` (next to `projectKey` in `lib/useProject.ts`'s pattern) is the one source of its key. The switcher's and the Projects page's inline queries are replaced by `useProjects()`. Every `invalidateQueries({ queryKey: ["projects"] })` uses `projectsKey`. Afterwards, `git grep -n '"projects"\]' -- frontend/src` finds only `projectsKey`'s definition.
- `Sidebar` and `ProjectSwitcher` use only this hook. On app pages the Project group stays, and its links go to that project's pages.
- The home page (`/`) keeps today's behavior.
- The module store has a reset function that the tests call between tests.

**Tests (stop condition):**
- On `/providers`, after opening project B (latest `last_opened_at`), the sidebar shows the Project group with links to `/projects/<B>/...`, and the switcher names B.
- On `/providers` with no project ever opened: no Project group, and the switcher reads "Projects".
- Inside `/projects/<A>/agents` the current project is A, whatever `last_opened_at` says. From there, with B as last opened, clicking AI providers keeps A.
- `npm test` (nothing else heavy running), lint, format check, build; CI green.

---

### Task 2: The sidebar header

**Files:** `frontend/src/app/shell/Sidebar.tsx`, `frontend/src/app/shell/ProjectSwitcher.tsx`, their tests.

**Interfaces:** Consumes task 1. Produces none.

**Cause:** the header is one row, 224px wide inside its padding. The app name (`shrink-0`) and the switcher share it, and the switcher is capped at `max-w-[130px]`, so a project name shows as "agentic MLO…" in a small pill.

**Implementation notes:**
- The app name sits on its own line at the top of the sidebar.
- Under it, the project switcher spans the sidebar's full width:
  - a full-width trigger showing the project's name (truncated with an ellipsis only when it doesn't fit) and the chevron;
  - the trigger is the same height as the nav items below it;
  - remove `max-w-[130px]`.
- The dropdown's width matches the trigger's.

**Tests (stop condition):**
- The switcher trigger has no `max-w-[130px]`, carries the full project name as its accessible name, and is a sibling below the app name, not beside it.
- Checked by hand and stated in the PR: a long project name reads in full up to the sidebar's width.
- `npm test`, lint, format check, build, `npm run build && npm run e2e` (the names in the overview's shared-files section stay); CI green, E2E included.
