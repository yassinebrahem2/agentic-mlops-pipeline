# Frontend: Every Page Has the Same Frame, Every Menu Entry an Icon Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** two defects found in the lead's walkthrough of the app (2026-11-02). (1) Nine pages stick to the sidebar: their outer wrapper is `container max-w-* py-6`, and Tailwind 4's `container` adds no side padding and no centering, so the content touches the sidebar's border (Dashboard, Inbox, Data, Models, Runs, Deployments, Activity, Knowledge, the incident page). The other pages set `mx-auto … p-6` themselves, each in its own words: the page frame has no single source. (2) Agents and Project settings are the only sidebar entries without an icon, so Agents is easy to miss: `nav.icon` is optional in `PageRoute`.

**Branch:** `plan/ui-page-frame`. **Milestone:** Stage 2: Agent runtime. **Consumes:** merged code. **Produces:** `PageFrame` (`frontend/src/components/PageFrame.tsx`), used by every page; `PageRoute.nav.icon` becomes required. The plan PR says `Closes` the plan issue and its task issue.

**Read first:** `frontend/src/app/routes.ts` (`PageRoute`), `app/shell/Sidebar.tsx` (`NavItem`), `app/shell/AppShell.tsx` (`<main>`); the outer wrapper of each `pages/*/…Page.tsx`.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | One page frame; an icon on every menu entry | merged code |

---

### Task 1: One page frame; an icon on every menu entry

**Files:** `frontend/src/components/PageFrame.tsx` (new) and its test; every page component's wrapper, in every return (loading, error and not-found states included: `IncidentPage.tsx` alone has four), under `frontend/src/pages/` (the nine above and those that already pad themselves: Projects, Add project, Onboarding step, AI providers, Integrations, App settings, Usage, Stats, Project settings); `frontend/src/app/routes.ts` (`icon` required); `pages/agents/route.tsx` and `pages/project-settings/route.tsx` (their icons, from `lucide-react` like the others); any route test fixture that builds a `PageRoute` with `nav` and no icon.

**Notes:**
- `PageFrame` is the one source of a page's side and top padding, centering and maximum width (a width prop with the sizes pages use today); each page's own wrapper classes for these go. Pages keep their inner layout.
- The Agents page is full height (its session view fills the screen): it keeps its own layout, only its empty and loading states use the frame if they need it.
- `icon` becomes required in `PageRoute.nav`, so a page without an icon no longer builds.

**Tests (stop condition):** `PageFrame` centers its content and pads it on every side; the sidebar shows an icon for every entry (Agents and Project settings included); a page's loading or error state is framed too; the existing page tests still pass; the PR description has a screenshot of the Inbox and the Dashboard next to the sidebar, at a desktop width and a phone width; frontend tests, lint, format, build; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
