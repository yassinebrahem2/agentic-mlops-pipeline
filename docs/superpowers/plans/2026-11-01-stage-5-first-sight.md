# Stage 5: A Model's First Sighting Is Not a Deploy Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** bug #649. The deployment rows a tracking source's first successful sync records describe where the aliases already were, not moves; today they are stored like moves, so the post-deploy trigger wakes Deployment & Rollback for the champion the tracker already held (the cursor is seeded before the first sync, so the rows' `seen_at` is after it), and the suspect linker logs them as recent `deployment` changes, so an incident in the first window gets a deploy suspect that never happened. Onboarding registers the source after the project exists, so this is every project's path, the demo's included.

**Branch:** `plan/stage-5-first-sight`. **Milestone:** Stage 5: Action agents. **Consumes:** merged code. **Produces:** `DeploymentRow.baseline: bool = False` (the stage 3 overview's `DeploymentRow` line, changed with this plan). The plan PR says `Closes` the plan issue, its task issue and #649.

**Read first:** bug #649; `backend/src/mlops_agents/tracking/store.py` (`_save_deployments`) and where a sync records `last_sync`; `tracking/contract.py` (`DeploymentRow`), `tracking/queries.py` (`query_deployments`); every reader of deployment rows: `promotion/post_deploy.py`, `monitoring/incidents/suspects.py`, `monitoring/hooks/alias_check.py`, `promotion/deployments.py`; `storage/migrations/project/` (bring `main` in first: PR #637 adds a project migration).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Rows from a source's first sync are its baseline | merged code |

---

### Task 1: Rows from a source's first sync are its baseline

**Files:** `backend/src/mlops_agents/tracking/{store,contract,queries}.py`; a new project migration (the next free number after `main`'s, `baseline` integer not null default 0 on `deployments`); `promotion/post_deploy.py`; `monitoring/incidents/suspects.py`; `monitoring/hooks/alias_check.py` and `promotion/deployments.py` only where they mean "a model moved"; tests under `backend/tests/tracking/`, `backend/tests/promotion/`, `backend/tests/monitoring/`.

**Notes:**
- **One place decides.** `_save_deployments` marks a row `baseline` when the source has no earlier successful sync (its `last_sync` is unset before this one). Not "no earlier rows": a source whose first sync found no alias, and whose model gets its first production alias later, records a real first deploy. Every reader reads the flag; none re-derives it.
- **Readers.** The post-deploy trigger and the suspect linker skip baseline rows. A check of a hook's alias move (`alias_check.py`) and the Deployments view keep them where they show the current champion (the view shows a baseline row as the starting champion, not as a deploy); the owner says in the PR what each reader does with it.
- **Not the bug's suggested fix.** Skipping rows whose `from_version` is `None` would silence a real first deploy of a new model; a test pins it.
- A source removed and added again starts a new baseline (its first sync again).

**Tests (stop condition):** first, one that fails on `main`: the order from #649 (the observer's first run, then the source's first sync recording the champion, then a move 1 → 2) gives exactly one `post_deploy` wake, for the move; a new model whose first production alias appears after the source's first sync wakes once; a source whose first sync saw no alias, then a first alias, wakes once; a restart re-wakes nothing; a baseline row is never stored as a recent change, so no incident gets it as a suspect; a database from before the migration reads its rows as not baseline; the stage 5 end-to-end test `test_fix_and_promotion.py::test_alias_move_wakes_deployment_once` passes once it is on `main`; backend suite, ruff, pyright; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
