# Frontend: A Failed Tool Step Shows It While Collapsed Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** from the lead's walkthrough 2 (2026-11-03). A tool step whose result is an error looks the same as a successful one until it is expanded (`ToolStep`'s error styling is only in the expanded body), so the person cannot see that an agent keeps failing: Onboarding failed `Propose` 27 times and every step looked fine.

**Branch:** `plan/tool-step-failed`. **Milestone:** Stage 2: Agent runtime. **Consumes:** merged code. **Produces:** nothing other plans use. The plan PR says `Closes` the plan issue and its task issue.

**Read first:** `frontend/src/pages/agents/ToolStep.tsx` (the header and its "Running" badge), `frontend/src/components/ui/badge.tsx` (`destructive`), how `AlertTriangle` is used in `frontend/src/pages/dashboard/StageCard.tsx`.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | A failed step's header says Failed | merged code |

---

### Task 1: A failed step's header says Failed

**Files:** `frontend/src/pages/agents/ToolStep.tsx`; `frontend/src/pages/agents/MessageView.test.tsx` (or a `ToolStep.test.tsx` beside it).

**Notes:**
- When `result.is_error`, the collapsed header shows a "Failed" badge (`destructive` variant) where the "Running" badge goes, with a warning icon inside it before the word (`AlertTriangle`, `text-destructive`, `aria-hidden`, as in `StageCard.tsx`). Nothing else changes: the card's border and background, its collapsed default and the expanded error box stay as they are.
- A call the person rejected or the policy denied also shows Failed (`tool_step.py` stores it as an error result): intended, the tool did not run.

**Tests (stop condition):** first, one that fails on `main`: a step with an error result shows "Failed" and the icon without being expanded; a successful step shows neither; a step with no result still shows "Running"; frontend tests, lint, format, build; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
