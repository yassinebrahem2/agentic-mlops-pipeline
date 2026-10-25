# Stage 2 — Runtime Fixes Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** Fix the defects found in the reviews of #232, #235 and #246: a turn's end can be lost (#242), provider keys are read twice in a file past the size limit (#236), and the status board tests carry inline suppressions (#247).

**Branch:** `plan/stage-2-runtime-fixes`. **Consumes:** merged code. **Produces:** nothing other plans use.

**Read first:** bug issues #242 and #236; `AGENTS.md` Code quality; the stage 1 spec §5 (Turns, Recovery).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Every turn reaches its end (#242) | merged code |
| 2 | One owner for provider key values (#236) | merged code |
| 3 | No inline suppressions in the status board tests (#247) | merged code |

---

### Task 1: Every turn reaches its end (#242)

**Files:** `backend/src/mlops_agents/agents/runtime/worker.py` (split first: it is at 399 lines), `executor.py`, and their tests under `backend/tests/agents/runtime/`. The approvals plan edits `executor.py` and the worker too (waiting turns): bring in `main` before you start and again before the PR.

**Interfaces:** Consumes merged code. Produces none.

**Implementation notes:**
- Move the turn-ending logic out of `worker.py` into its own module (for example `runtime/turn_end.py`), so the worker keeps the loop and every file stays well under 400 lines.
- Each of these must lead to a stored end, the observers hearing it, `turn.ended` published to connected subscribers, and the worker moving on to the next queued turn:
  - a storage error inside the interrupted path, with the error logged and the end still recorded through one shielded fallback that cannot raise again;
  - a cancel of the turn task that is neither Stop nor close (the turn ends `interrupted`);
  - a failure while writing the failure notice: exactly one notice per failed turn.
- A Stop or close that lands during the executor's failure path still marks the agent message `stopped`.

**Tests (stop condition):** one test per case above, each with a second turn queued that then runs; the runtime tests pass 10 times in a loop; full backend suite, ruff, format, pyright; CI green on Linux and Windows.

---

### Task 2: One owner for provider key values (#236)

**Files:** a new `backend/src/mlops_agents/providers/keys.py`, `backend/src/mlops_agents/providers/service.py`, and their tests.

**Interfaces:** Consumes merged code (`ProvidersService.keys_for` and `secret_values`, used by the audit log). Produces none; both methods keep their signatures.

**Implementation notes:**
- `providers/keys.py` owns stored keys:
  - their refs and order;
  - reading their values from the keystore (one reader, used by both `keys_for` and `secret_values`);
  - the cached secret values and their invalidation on add, remove or rotate.
- `service.py` calls it and shrinks below 400 lines.

**Tests (stop condition):** `keys_for` and `secret_values` behave as before, the cache is invalidated on each key change, and `service.py` is under 400 lines; full backend suite, ruff, format, pyright; CI green.

---

### Task 3: No inline suppressions in the status board tests (#247)

**Files:** `backend/tests/events/test_status.py`, and the shared session fake under `backend/tests/` if one fits.

**Interfaces:** Consumes merged code. Produces none.

**Implementation notes:** the fake passed to `bind_sessions` satisfies `AgentSessions` (reuse the shared fake if there is one), so the two `pyright: ignore[reportArgumentType]` comments go.

**Tests (stop condition):** `git grep -n "pyright: ignore" -- backend/tests/events` finds nothing; full backend suite, ruff, format, pyright; CI green.
