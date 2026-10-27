# Stage 2 — Inbox Fixes Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, interfaces, tests (its stop condition) and checks.

**Goal:** Fix bug #290, found in the reviews of #285.

**Branch:** `plan/stage-2-inbox-fixes`. **Consumes:** merged code. **Produces:** the `Switch` component (`components/ui/switch.tsx`), which later plans may reuse.

**Read first:** bug #290; `AGENTS.md` Code quality (one source for every type and helper).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Inbox and settings review fixes (#290) | merged code |

---

### Task 1: Inbox and settings review fixes (#290)

**Files:** new `frontend/src/components/ui/switch.tsx` (and its test); `frontend/src/pages/project-settings/SandboxSection.tsx`, `AgentsSection.tsx` and their tests; `frontend/src/pages/inbox/InboxPage.tsx`, `LocalReviewCard.tsx`, `usePendingApprovals.ts`; `frontend/src/lib/useProjectEvents.test.ts`.

**Interfaces:** Consumes merged code. Produces `Switch`: `checked`, `onCheckedChange`, `disabled`, an accessible name (`aria-label` or a `<label>`), styled like the other `components/ui` controls (focus ring, pointer cursor, disabled state).

**Implementation notes:**
- **Switch.** No switch exists in `components/ui/`. Add one, built the way the existing ui components are, on the Switch primitive of the `radix-ui` package the project already depends on (no new dependency). Use it for both plain checkboxes in project settings: the sandbox "Network access" toggle (`SandboxSection.tsx:120`) and "No limit" (`AgentsSection.tsx:207`). Tests that query `getByRole("checkbox", ...)` move to `getByRole("switch", ...)`.
- **No query without a project.** `InboxPage.tsx:145` passes `projectId ?? ""`. The page renders its sections only once the route has a project ID, so no child receives `""`. The same holds for every hook the Inbox and settings pages call: a hook that needs a project is never called with an empty ID.
- **One `LocalReview` type.** It is declared in both `LocalReviewCard.tsx:11` and `usePendingApprovals.ts:8`. Keep the export in `usePendingApprovals.ts` (it owns the data) and import it in the card.
- **One error text.** `InboxPage.tsx` lines 67, 129 and 174 format errors themselves (`x instanceof Error ? x.message : "..."`). Use `errorText` from `lib/api.ts`, as the settings sections do.
- **`useProjectEvents` lifecycle tests.** The code already handles both cases (re-review of #285), so these are tests only: unmounting the last subscriber while the backoff timer is pending opens no stream afterwards; under `StrictMode` the double mount ends with exactly one open stream.

**Tests (stop condition):**
- `Switch`: toggles by click and by Space, has `role="switch"` and `aria-checked`, and does nothing while disabled.
- Both settings toggles still save as before, through the switch.
- The Inbox renders no section and calls no project route while no project is selected.
- Inbox load errors show the text `errorText` gives.
- The two `useProjectEvents` tests above.
- `npm run lint`, `format:check`, `npm test`, `npm run build`, `npm run e2e`; CI green on every job.
