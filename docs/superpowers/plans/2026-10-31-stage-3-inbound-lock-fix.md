# Stage 3: Inbound Lock and Hub Fix Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** bug #573. The inbound source's mid-turn take and its redelivery run under the session's own lock, and publish to the session's own hub. Today both fall back to a private lock and hub (the `getattr` calls find nothing), so they don't serialise with Send, `get`, Stop and the worker, and their message updates never reach the live stream. The risk: this moves an uncontended lock onto the busiest lock in the runtime, days before the deadline, so the deadlock audit below is the gate.

**Branch:** `plan/stage-3-inbound-lock-fix`. **Consumes:** merged code (#574). **Produces:** nothing other plans use; `AgentSessions` keeps its signatures. The plan PR says `Closes` the plan issue, its task issue and #573.

**Read first:** bug #573; `backend/src/mlops_agents/agents/messaging/inbound.py` (`_get_lock`, `_get_hub`, `take`, `turn_ended`, `bind`); the session registry that owns each session's lock and hub (`self._registry.lock(...)` and `self._registry.hub(...)` in `agents/runtime/sessions.py`); `api/services.py` (where both are built and bound).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | The inbound source uses the session's lock and hub | merged code |

---

### Task 1: The inbound source uses the session's lock and hub

**Files:** `backend/src/mlops_agents/agents/messaging/inbound.py`; the narrow protocol it depends on (beside `QueuedTurns`); `backend/src/mlops_agents/api/services.py` (its binding lines); tests under `backend/tests/agents/`.

**Notes:**
- **One owner.** Each session's lock and hub have one owner, the session registry. The inbound source reads both from it, through a narrow protocol bound as today (extend `QueuedTurns` or add a sibling; the owner's choice). The private `_locks`/`_hubs` fallback and both `getattr` calls go away. An unbound source fails loudly, never silently making a lock of its own; binding happens in `build_services` before anything that can run a turn (restart repair, startup delivery, the scheduler, `get`), with a `build_services` test that it is bound.
- **Deadlock audit (the gate).** `take` runs in the turn's own task (`TurnWorkers._child_tasks`), not the worker's, and `AsyncRLock` re-enters only within one task. The graph and the executor take the session lock themselves (graph.py ~108, 162, 181, 188, 282; executor.py ~83, 180, 240, 356). The risk is a task that holds the session lock while it waits on the turn task, or on something the turn task does, when the turn calls `take`. List every place that holds the session lock and waits on the turn task or its work: start with `_take_turn` and `_run_turn`, the executor's locked blocks, `turn_end.py`, `_WaitAwareSink` and the waiting path, and whoever calls `TurnWorkers.cancel`. Put the list in the PR. If any such place exists, open a Decision and stop; don't reshape the runtime to fit.
- **Construction order.** If the registry is built after the inbound source, bind it the way `QueuedTurns` is bound; don't reorder unrelated services.

**Tests (stop condition)**, deterministic (events or fakes that hold a step, no sleeps):
- First, a test that fails on `main`: both publishing paths reach a subscriber of the session's live stream (`subscribe`): the mid-turn take's status change and the redelivery's.
- A mid-turn take and a Send for the same session never interleave: the take holds the session lock (hold a step and assert the other writer waits).
- A mid-turn take through a real turn (worker, turn task, the graph's take) with the registry's lock completes; the test has a timeout that fails rather than hangs.
- The #570 and #574 tests stay green. Full backend suite, ruff, pyright; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
