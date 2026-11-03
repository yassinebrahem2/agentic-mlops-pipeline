# Frontend Fixes (walkthrough 3) Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. **Every task is its own early PR** (`AGENTS.md` rule 5): work it on `plan/frontend-walkthrough-fixes-task<n>`, created from `origin/main`, and open its PR into `main` when done (`Closes #<task issue>`). There is no plan branch and no plan PR: the plan issue closes when its last task's PR merges. Tasks 1-3 start now; tasks 4 and 5 start once what they consume is on `main`.

**Goal:** what the person saw in the lead's walkthrough 3 (2026-11-03; fix list items 10, 13, 14, 22, 27, 30): agent messages read as a wall of text; opening another agent's chat replays a scroll from the top; the model card shows `0.8538461538461538`; an agent stopped by its step limit looked idle; expectations nobody had judged showed "ok"; the team's fired checks were nowhere on the Dashboard.

**Branches:** one per task, `plan/frontend-walkthrough-fixes-task<n>`. **Milestone:** Stage 2: Agent runtime. **Consumes:** task 4: `SessionView.last_stopped_reason` and `TurnEndedData.stopped_reason` (agent runtime fixes plan, task 4); task 5: `ExpectationState.status` `"not_judged"` and `DashboardView.team_alerts` (monitoring and proposal fixes plan, task 6). **Produces:** nothing.

**Review bar:** the lowered bar.

## Tasks

| Task | Title | Fix | Consumes |
|---|---|---|---|
| 1 | Agent messages read like a chat app | 13 | merged code |
| 2 | A conversation opens at its last message, with no scroll animation | 14 | merged code |
| 3 | Numbers are rounded for display | 27 | merged code |
| 4 | A turn stopped by the step limit says so | 10 | agent runtime fixes task 4 |
| 5 | The Dashboard shows "not judged yet" and the team's fired checks | 22, 30 | monitoring and proposal fixes task 6 |

---

### Task 1: Agent messages read like a chat app

**Files:** `frontend/src/pages/agents/MessageView.tsx` (the markdown components); its tests.

**Notes:** typography like ChatGPT or claude.ai: body text one step larger than today, relaxed line height, clear space between paragraphs, lists and sections (paragraphs and list items no longer `my-1.5` / `space-y-0.5`), headings at visibly different sizes (h1 > h2 > h3 > body), tables and code blocks with room around them. Tool steps keep their compact style.

**Tests:** the rendered markdown uses the new heading and spacing classes (one test per element kind is enough); frontend tests, lint, format, build; a screenshot of one long agent message in the PR.

### Task 2: A conversation opens at its last message, with no scroll animation

**Files:** `frontend/src/pages/agents/MessageList.tsx`; its tests.

**Notes:** remove the smooth scroll (`MessageList.tsx:50-54`). On opening a conversation (or switching agents) the list is at the bottom on first paint, with no visible scroll; a new message while the person is at the bottom keeps them there instantly; a person who scrolled up is not moved.

**Tests:** switching sessions renders at the bottom without `behavior: "smooth"` (fails on `main`); scrolled-up stays put on a new message.

### Task 3: Numbers are rounded for display

**Files:** a shared formatter in `frontend/src/lib/` (e.g. `formatMetric(value: number): string`); `frontend/src/pages/dashboard/ModelCards.tsx` and every other place that prints a raw metric value (search for them); tests.

**Notes:** 3 significant digits (`0.854`, `12.3`, `1,234`), integers as integers; one formatter used everywhere a metric or result value is shown.

**Tests:** the formatter's cases; the model card shows `0.854` for `0.8538461538461538`.

### Task 4: A turn stopped by the step limit says so

**Files:** the agent chat header or composer area in `frontend/src/pages/agents/` (and the agent list status if it shows idle); tests.

**Notes:** when the session's latest turn has `last_stopped_reason` `"max_steps"`, show "Stopped at its step limit: send a message to continue" near the composer; it goes away when a new turn starts. `"room_below_floor"`: "Stopped: its context is full".

**Tests:** shown for a stopped session, hidden for a normal one and once a new turn starts.

### Task 5: The Dashboard shows "not judged yet" and the team's fired checks

**Files:** `frontend/src/pages/dashboard/AtRiskList.tsx` (and `pages/project-settings/ChecksSection.tsx` where it shows status); a `TeamAlerts` component in `frontend/src/pages/dashboard/`, placed on `DashboardPage.tsx`; tests.

**Notes:** `not_judged` shows as "Not judged yet" (neutral, not green). `team_alerts` is a "Your team's alerts" card: check, subject, when it ran, and whether an agent reacted (its verdict), newest first; hidden when empty.

**Tests:** each state renders; the card lists alerts and hides when there are none.

## Known debt added

None planned.
