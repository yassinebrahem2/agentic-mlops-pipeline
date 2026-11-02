# Stage 4: A Failed Source Is Read Again Once Fixed Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. Two tasks in order, one PR for the branch, no early PR (`AGENTS.md` rule 5). Each task lists its files, tests (its stop condition) and checks.

**Goal:** bugs #701 and #702, from the lead's walkthrough (2026-11-02). (1) A file results source whose lines were rejected because of the source itself (no parser for team lines, a parser that fails) moves its cursor past them anyway, so approving a parser later stores nothing and the history is lost for good; the failure says "origin must be 'agent', found None" where the user needs "these are the team's records: propose a parser". (2) An agent can resolve a `source_failure` or `parser_failure` incident while the source still fails, because the resolve guard looks only at silence and fired results.

**Branch:** `plan/stage-4-results-rereading`. **Milestone:** Stage 4: Monitoring loop. **Consumes:** merged code. **Produces:** nothing other plans use. The plan PR says `Closes` the plan issue, its task issues, #701 and #702.

**Read first:** bugs #701, #702; `backend/src/mlops_agents/monitoring/results/file_source.py` (the per-file cursor, the origin check), `results/store.py` (dedup on `(source_id, source_key)`), where a parser proposal is applied (`services.parsers.store` and its kind's `apply()`); `monitoring/incidents/service.py` (`update`'s resolve guard) and how a source's failure state is kept (`result_sources`, the parser failure count).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Rejected lines are read again once their source is fixed | merged code |
| 2 | A failing source keeps its incident from resolving | merged code |

---

### Task 1: Rejected lines are read again once their source is fixed

**Files:** `backend/src/mlops_agents/monitoring/results/file_source.py`; the parser apply path (the one place a parser version is stored); tests under `backend/tests/monitoring/results/`.

**Notes:**
- A file's cursor does not move past lines rejected for a source-level cause (no parser for lines that are not the agents' own records, a parser error); lines rejected one by one for their own content (a malformed line) still move it, as today.
- Storing a new parser version for a source resets that source's cursor, so its files are read again from the start; records already stored are not stored twice (the store's dedup on `(source_id, source_key)`).
- A line without `origin: "agent"` in a source with no parser fails with a message that says these look like the team's own records and a `parser` proposal reads them.

**Tests (stop condition):** first, one that fails on `main`: team lines in a source with no parser fail and leave the cursor; approving a parser for that source then stores every earlier record once; a second read stores nothing new; a single malformed line in an agent source is skipped as today; backend suite, ruff, pyright; CI green.

### Task 2: A failing source keeps its incident from resolving

**Files:** `backend/src/mlops_agents/monitoring/incidents/service.py` (`update`'s resolve guard only); tests under `backend/tests/monitoring/incidents/`.

**Notes:**
- An incident with a `source_failure` or `parser_failure` signal resolves only when that source's latest read succeeded (its failure state is clear), read from the results module's own query, never its tables; otherwise `incident_still_firing`, as for the other signals. Plan #695 task 1 changes the grouping call in this file's `raise_signal`; this task touches only `update`.

**Tests (stop condition):** first, one that fails on `main`: a source failure incident cannot be resolved while the source still fails, and can once a read succeeds; the same for a parser failure; silence and fired-result guards unchanged; backend suite, ruff, pyright; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
