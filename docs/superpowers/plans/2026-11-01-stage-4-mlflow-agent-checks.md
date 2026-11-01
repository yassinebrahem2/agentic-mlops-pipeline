# Stage 4: Agent Checks with MLflow Results Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** bug #628. An agent check registered with `RegisterCheck` and an MLflow results location produces records under its own `check_id`, with its category, `origin: "agent"`, its version, threshold and `fires_when`, so its expectation is met on each run (no false silence alarm at every cadence) and confirmation by labels can read it. Today `MlflowSource` takes these only from the handoff, so the records carry the source ID and `origin: "tracker"`.

**Branch:** `plan/stage-4-mlflow-agent-checks`. **Milestone:** Stage 4. **Consumes:** merged code (the results plan, #609). **Produces:** nothing other plans use. The plan PR says `Closes` the plan issue, its task issue and #628.

**Read first:** bug #628 (the code path, with lines); `backend/src/mlops_agents/monitoring/results/mlflow_source.py`; `monitoring/expectations/checks.py` (`ChecksStore`, how an expectation is keyed by the registered `check_id`); `monitoring/expectations/service.py` (`note_seen`, `keys_for_result`); `monitoring/verdicts/store.py` (`is_label_evidence`); `agents/tools/register_check.py`; `proposals/payloads.py` (`MlflowResults`: `metric`, `threshold`, `fires_when`).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | An MLflow-located agent check's records are its own | merged code |

---

### Task 1: An MLflow-located agent check's records are its own

**Files:** `backend/src/mlops_agents/monitoring/results/mlflow_source.py`; the narrow protocol it reads registered checks through (beside the source, or in `monitoring/expectations/checks.py` next to `ChecksStore`; the owner's choice); `api/services.py` (its wiring line); `agents/tools/register_check.py` and the place it is built (it gets `Handoffs.current`, I20, beside its `ChecksStore`, to see the handoff's checks); tests under `backend/tests/monitoring/results/` and `backend/tests/agents/tools/`.

**Notes:**
- **One source.** The checks store owns registered checks. `MlflowSource` reads the project's **active** registered checks through a narrow read-only protocol; it never copies them. For a metric that an active registered check names in its `MlflowResults`, the records take that check's `check_id`, category, `origin: "agent"`, version, and its location's `threshold` and `fires_when`. A pending check (its PR not merged) is left out.
- **Everything else unchanged.** A metric no registered check names gives byte-identical records to today, handoff checks and the handoff's results location included.
- **One metric, one check.** `RegisterCheck` refuses (a tool error saying which check already uses it) an MLflow metric that a handoff check, the handoff's MLflow results location, or another pending or active registered check already uses, so two checks never fight over one source. The refusal never applies to the same `check_id` registering the same metric again (re-registering a check whose PR already merged stays as #609 left it). Registration enforces this one way only: a later handoff can still name a metric an active registered check uses; then the registered check wins for that metric, decided in one place in `MlflowSource`, with a logged warning (the handoff check does not warn about it yet: known debt, recorded at merge).
- The e2e fixture uses a file results location only, so these tests are the only cover for this path.

**Tests (stop condition):**
- First, one that fails on `main`: an active registered check with an MLflow location, whose metric is logged on its cadence, meets its expectation (no silence signal), and its records carry its `check_id`, category, `origin: "agent"`, version and threshold, and fire when the value crosses it.
- Its records pass `is_label_evidence` when its category is `model_performance`.
- A pending registered check does not change the records.
- An unregistered metric gives the same records as on `main`.
- `RegisterCheck` refuses a metric already used by a handoff check, by the handoff's results location, and by another registered check; re-registering an active MLflow check (same `check_id`, same metric) succeeds.
- A later handoff naming an active registered check's metric: the registered check's records win, and a warning is logged.
- Backend suite, ruff, pyright; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
