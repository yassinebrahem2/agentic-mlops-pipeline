# Stage 6: Stats Under a Category Filter Count Only That Category Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** the bug below (its issue is linked from the plan issue). With a category chosen on the Stats page, `misses` and `false_alarms_avoided` still count every category: `StatsService._inputs` (`stats/service.py`, ~85-98) filters incidents by category but takes every miss report (`list_misses`) and every judged silent team alert (`judged_silent_alerts`) whatever their category, so a person filtering by "drift" sees the misses and avoided false alarms of all categories. Found by the Stage 7 benchmark report (#676), where both numbers are identical in every category.

**Branch:** `plan/stage-6-stats-category`. **Milestone:** Stage 6: Stats, Activity, Knowledge, Usage. **Consumes:** merged code. **Produces:** `SilentTeamAlert.category: ResultCategory` (the stage 4 to 6 overview's I31 line, changed with this plan). The plan PR says `Closes` the plan issue, its task issue and the bug.

**Read first:** `backend/src/mlops_agents/stats/service.py` (`_inputs`), `stats/metrics.py` (`misses`, `false_alarms_avoided`); `monitoring/verdicts/contract.py` (`MissReport.category`, `SilentTeamAlert`) and `monitoring/verdicts/baseline.py` (where silent alerts are built from stored team results, which carry their category); the frontend's generated types (`npm run gen:api`).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | A category filter counts only that category's misses and silent alerts | merged code |

---

### Task 1: A category filter counts only that category's misses and silent alerts

**Files:** `backend/src/mlops_agents/stats/service.py`; `monitoring/verdicts/contract.py` (`SilentTeamAlert.category`); `monitoring/verdicts/baseline.py` (fill it from the stored result); `frontend/openapi.json` regenerated, and any frontend fixture of a `SilentTeamAlert` (add `category`); tests under `backend/tests/stats/` and `backend/tests/monitoring/verdicts/`.

**Notes:**
- Under a category, `miss_reports` counts misses whose `category` is that category; a miss reported without a category counts only when no category is chosen (it cannot be placed). Judged silent alerts are kept by their `category` the same way. With no category chosen, nothing changes.
- `SilentTeamAlert.category` comes from the team result the alert is built from (it always has one); nothing re-derives it.

**Tests (stop condition):** first, one that fails on `main`: two misses (one `drift`, one `data_quality`) and two judged silent alerts of those categories give `misses` 1 and `false_alarms_avoided` counted for one alert under each category, and both under no category; a miss with no category counts only without a filter; the baseline router returns `category` on silent alerts; frontend build and tests; backend suite, ruff, pyright; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
