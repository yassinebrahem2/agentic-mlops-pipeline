# Stage 2 — Frontend Inbox Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** One place for everything waiting on the user (spec §10):
- the Inbox page: approvals, local reviews, and the agents' PRs and issues;
- a badge on its sidebar entry;
- the project settings for permissions, sandbox, issues and messaging.

**Branch:** `plan/stage-2-frontend-inbox`. **Consumes:** I9 and I11 (generated types); the routes in the overview's table for approvals, reviews, GitHub activity and the four settings, plus `GET /sandbox` and `POST /sandbox/rebuild`; project-context merged. **Produces:** nothing other plans use.

**Read first:** spec §6 (local reviews) and §10; the overview's routes table and global constraints.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | The Inbox page and its badge | runtime-contracts task 1, project-context merged; merging: approvals task 4, github-work task 4 |
| 2 | Project settings sections | task 1; merging: approvals task 1, sandbox task 3, github-work task 3, messaging task 1 |

---

### Task 1: The Inbox page and its badge

**Files:**
- new `frontend/src/pages/inbox/route.tsx`, `InboxPage.tsx`, `LocalReviewCard.tsx`;
- `frontend/src/app/routes.ts` and `Sidebar.tsx`: an optional `badge` hook on a nav entry, added once for any page to use;
- tests.

**Implementation notes:**
- The Inbox is a project-level page, with the nav label "Inbox" and its icon.
- Approvals: every pending approval across agents, using frontend-agents' `ApprovalCard`. Import it; never copy it. If frontend-agents task 2 has not merged, this section waits for it.
- Changes to merge: each local review shows its agent, the task title, the list of the user's files that will change ("These files in your folder will change"), the diff, Approve and Reject with a note. A 409 shows its message.
- Pull requests and issues: `GET /github/activity` as a list with kind, number, title, state, agent and an external link.
- The badge counts pending approvals plus local reviews, refreshed by the project event stream (`lib/useProjectEvents.ts`).

**Tests (stop condition):**
- Each section renders from stubbed routes, and empty sections say so.
- Approve and Reject on a local review call the route, and a 409 shows.
- The badge counts and updates on an event.
- `npm test` (nothing else heavy running), lint, format check, build; CI green.

---

### Task 2: Project settings sections

**Files:** new `frontend/src/pages/project-settings/PermissionsSection.tsx`, `SandboxSection.tsx`, `IssuesSection.tsx`, `MessagingSection.tsx`; `ProjectSettingsPage.tsx` (adds the four); tests.

**Implementation notes:**
- **Permissions:** the level as a `Select` with one line explaining each level. Choosing `bypass` opens a confirmation dialog first.
- **Sandbox:** Docker status with its message, the environment image's state and detail, Rebuild (disabled while building), and the network switch.
- **Issues:** the minimum priority.
- **Messaging:** the hop and token limits as numbers, validated before saving.
- Each section saves through its own route and shows the server's error text.

**Tests (stop condition):** each section loads and saves; bypass needs the confirmation; Rebuild is disabled while building; checks; CI green.
