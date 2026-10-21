> **DRAFT. Do not implement.** Still missing: an advisor review round with no blocking issues. The lead removes this banner when the plan leaves `drafts/`.

# Stage 1 — Frontend Workspace Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. Each task is one PR into `main` (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** The pages for projects: the Projects home with the project switcher, adding a project (local folder with a folder browser, or GitHub by URL or from the user's repositories), and a project's Settings (repository info, refresh, relink, remove, per-agent model and step limit).

**Branch:** `plan/stage-1-frontend-workspace`. **Consumes:** I5 (frontend-foundation tasks 1 and 2), I6 (api-resources). **Produces:** the pages at `/`, `/projects/new`, `/projects/:projectId` and `/projects/:projectId/settings` (I5's page paths).

**Read first:** the stage overview (global constraints, I5, I6, the stage 1 end-to-end flow steps 1 and 3), spec sections 2 ("Workspace operations") and 6, `AGENTS.md` (Frontend commands), and the merged `WorkspaceService` for the error codes below.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Projects home, project switcher, project landing | I5 on `main`; api-resources task 1 (#50) |
| 2 | Add project | task 1; api-resources tasks 1 and 2 (#50, #51) |
| 3 | Project Settings | task 1; api-resources tasks 1, 3 and 4 (#50, #52, #53) |

## Rules for every page

- A page is a folder under `frontend/src/pages/` whose `route.tsx` exports only `route` (I5); its components live in other files of that folder.
- Data comes from `api` through TanStack Query (`useQuery`/`useMutation` with `unwrap`); query keys start with the resource: `["projects"]`, `["projects", id]`, `["folders", path]`, `["github"]`, `["github", "repos"]`, `["models"]`, `["projects", id, "agents"]`, `["app"]`. A mutation invalidates the keys it changes.
- Errors are shown with `errorText(error)` next to the action that failed. When a page reacts to a specific `ApiError.code`, it is one of the codes listed in the task (read from the merged services), never a guessed one.
- Every control has a visible label or accessible name: the e2e plan drives the pages by role and label, without test ids.
- Tests use the foundation's `renderPage` helper (I5) with a stubbed `fetch`; they assert what the user sees and which requests were sent.

---

### Task 1: Projects home, project switcher, project landing

**Files:**
- Create: `frontend/src/pages/projects/route.tsx`, `ProjectsPage.tsx`; `frontend/src/components/ProjectStatus.tsx` (shared: tasks 2 and 3 and frontend-chat use it); `frontend/src/pages/project-home/route.tsx`, `ProjectHome.tsx`, `firstProjectPath.ts`; `frontend/src/app/shell/ProjectSwitcher.tsx`
- Modify: `frontend/src/app/shell/Sidebar.tsx` (render `ProjectSwitcher` in the sidebar header; frontend-foundation left that place to this plan)
- Test: `ProjectsPage.test.tsx`, `firstProjectPath.test.ts`, `ProjectHome.test.tsx`, `ProjectSwitcher.test.tsx` beside them

**Interfaces:**
- Consumes: I5 (`PageRoute`, `pageRoutes`, `api`, `unwrap`, `errorText`, `renderPage`); I6 `GET /projects`, `POST /projects/{id}/open`; the `Project` schema.
- Produces: `route` at `/` with `nav: { area: "app", label: "Projects", order: 0 }`; `route` at `/projects/:projectId` (no nav); `ProjectStatus` (a badge plus the detail text, reused by tasks 2 and 3).

**Implementation notes:**
- Projects: a table or list of projects with name, source ("Local folder" / "GitHub"), status and last opened (local time, spec "Time"), sorted by last opened, most recent first; an "Add project" button to `/projects/new`; an empty state ("No projects yet.") with the same button. Clicking a project opens it.
- Opening a project: `POST /projects/{id}/open` (it refreshes a GitHub clone, so it can take seconds: show progress on that row), then navigate to `/projects/:projectId`.
- `firstProjectPath(routes: PageRoute[], projectId: string): string | null`, a pure function: the first `project`-area route by `order`, with `:projectId` filled in. `ProjectHome` at `/projects/:projectId` redirects (replace) to `firstProjectPath(pageRoutes, projectId)`. With frontend-chat merged that is Agents (`order: 0`); before, it is Settings (`order: 100`).
- `ProjectStatus`: `ok` → nothing loud; `missing` → "Folder not found" and the detail; `empty` → "No committed files yet. Commit your files first."; `clone_failed` → "Download failed" and the detail (it already says to use Refresh). A refresh failure that kept the last copy arrives as `ok` with a `status_detail`: show the detail as a warning.
- `ProjectSwitcher`: a menu button showing the current project's name or "Projects". The current project comes from the matched route's params (`useParams()`), never from the URL text: `/projects/new` is not a project called "new" (it matches no route with `:projectId`). It it lists all projects (opening one goes through the same open-then-navigate path) and ends with "All projects" (`/`).

**Tests (behaviors to verify):** the list shows each project's name, source, status and last opened; the empty state; clicking a project sends `POST .../open` and lands on its Settings page (the project page on the branch); `firstProjectPath` picks the lower `order` among fake routes, fills in the id, and returns null when there is none; `ProjectStatus` texts for the four statuses and the "kept the last copy" warning; the switcher shows the current project and lists the others; at `/projects/new` the switcher shows "Projects"; a failing `GET /projects` shows `errorText`.

**Checks (from `frontend/`):** `npm run lint`, `npm run format:check`, `npm test`, `npm run build`.

---

### Task 2: Add project

**Files:**
- Create: `frontend/src/pages/add-project/route.tsx`, `AddProjectPage.tsx`, `LocalFolderTab.tsx`, `GitHubTab.tsx`; `frontend/src/components/FolderBrowser.tsx` (task 3's Relink reuses it)
- Test: `AddProjectPage.test.tsx`, `FolderBrowser.test.tsx`

**Interfaces:**
- Consumes: I6 `POST /projects`, `GET /folders`, `GET /github`, `GET /github/repos`, `GET /app` (the run mode); the error codes `not_a_git_repo` (`detail.can_init`), `not_repo_root` (`detail.repo_root`), `project_exists` (`detail.project_id`), `folder_not_found`, `folder_unreadable`, `invalid_github_repo`, `github_not_connected`, `local_projects_disabled`.
- Produces: `route` at `/projects/new` (no nav: reached from Projects); `FolderBrowser({ initialPath?, onChoose(path) })`.

**Implementation notes:**
- Two tabs, "Local folder" and "GitHub". In server mode (`GET /app` → `mode: "server"`) the local tab is not shown at all and the page says "This server works with GitHub repositories."
- `FolderBrowser`: shows the current folder's path, a "Up" button (the listing's `parent`), the drive roots when the listing has several (Windows), and the subfolders, each marked "git repository" when `is_git_repo`. Clicking a folder opens it. A "Folder path" text input with "Go" also navigates (typed paths, spec section 2). "Use this folder" calls `onChoose` with the current path. Errors (`folder_not_found`, `folder_unreadable`) show their message in place, and the browser stays usable.
- Local tab: `FolderBrowser` → `POST /projects {source: "local", path}`:
  - `not_a_git_repo` with `can_init` → a dialog with the server's message and "Run git init", which retries with `init_git: true`;
  - `not_repo_root` → the message and a button "Add <repo_root> instead";
  - `project_exists` → the message and a link "Open it" (`/projects/<project_id>`).
  - Success → navigate to `/projects/:projectId`.
- GitHub tab: when `GET /github` says `mode: "none"`, show "Connect GitHub in Integrations first." with a link to `/integrations` (I5 page path) and nothing else. Otherwise a "Repository" input (owner/name or URL) with "Add", and below it the user's repositories (`GET /github/repos`) with a filter box; choosing one fills the input. Adding a GitHub project clones it, which can take a while: show "Downloading <owner/name>..." and keep the button disabled until the response. A created project whose status is `clone_failed` still lands on its page, where `ProjectStatus` explains it.

**Tests (behaviors to verify):** server mode hides the local tab; the folder browser lists folders, goes up, opens a typed path, shows a `folder_not_found` message and stays usable; adding a non-git folder shows the dialog and the retry sends `init_git: true`; `not_repo_root` offers the root and adding it sends that path; `project_exists` links to the existing project; GitHub not connected shows only the link to Integrations; the repository list filters and fills the input; adding sends `{source: "github", repo}` and shows the progress text until the response; success navigates to the project.

**Checks:** as task 1.

---

### Task 3: Project Settings

**Files:**
- Create: `frontend/src/pages/project-settings/route.tsx`, `ProjectSettingsPage.tsx`, `RepositorySection.tsx`, `AgentsSection.tsx`, `RemoveProjectDialog.tsx`
- Test: `ProjectSettingsPage.test.tsx`

**Interfaces:**
- Consumes: I6 `GET /projects/{id}`, `POST .../refresh`, `POST .../relink`, `DELETE /projects/{id}`, `GET /projects/{id}/agents`, `PUT .../agents/{agent}/model`, `PUT .../agents/{agent}/limits`, `GET /models`, `GET /providers` (a model's provider kind); `FolderBrowser`, `ProjectStatus`; the codes `not_local`, `local_projects_disabled`, `model_not_usable`.
- Produces: `route` at `/projects/:projectId/settings` with `nav: { area: "project", label: "Settings", order: 100 }`.

**Implementation notes:**
- Repository: name, source, the local path or the GitHub repository, default branch, `ProjectStatus`; "Refresh" (`POST .../refresh`); for local projects in local mode "Relink" opens `FolderBrowser` in a dialog and posts the chosen path (the way to fix `missing`).
- Agents: one row per agent from `GET /projects/{id}/agents`: display name, "Model" select and "Step limit".
  - The model select offers "Use the app default (<default_model or none>)" and the usable models from `GET /models`: `availability == "verified"`, `tool_calling`, `streaming` and a known `context_window` (the same rule as I2, so the server never answers `model_not_usable` for a listed choice). Choosing sends `PUT .../model` with the address, or `null` for the default.
  - `model_problem`, when set, is shown under the row as a warning.
  - "Step limit": a number input (at least 1) and a "No limit" checkbox; saving sends `PUT .../limits` with the number or `null`.
  - Where an agent's effective model belongs to a `hosted` provider (look the provider up by `provider_name` in `GET /providers`), show under that row the spec's warning: "Files the agent reads are sent to <provider>. A secret inside a committed file whose name does not look secret can reach it." (spec section 5, known limitation). Not shown for self-hosted models.
- Remove: "Remove project" opens a dialog saying the app's copy, sessions and settings for this project are deleted and the user's own folder is not touched (for GitHub projects: the downloaded copy is deleted); confirming sends `DELETE` and navigates to `/`.

**Tests (behaviors to verify):** the repository section for a local and a GitHub project; Refresh sends the request and shows the new status; Relink is offered only for local projects and sends the chosen path; the model select lists only usable models plus the default option, and choosing sends the address or `null`; `model_problem` is shown; the step limit sends a number, and "No limit" sends `null`; the hosted-model warning is shown for a hosted effective model and absent for a self-hosted one; Remove asks first, sends `DELETE` and returns to `/`.

**Checks:** as task 1.

---

## Not in this plan

- The Agents (chat) page (frontend-chat), AI providers, Integrations and app Settings (frontend-providers).
