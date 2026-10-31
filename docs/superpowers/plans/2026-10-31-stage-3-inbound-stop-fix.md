# Stage 3: Inbound Stop Fix Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** bug #569. A quick second Stop no longer hits the turn of a redelivered agent-to-agent message. Today that turn ends `stopped` without its model being called. Its trigger is the message itself, not one taken mid-turn, so it is not redelivered again, and the message is lost.

**Branch:** `plan/stage-3-inbound-stop-fix`. **Consumes:** merged code (#570, the lost-stop fixes). **Produces:** nothing other plans use; `AgentSessions` keeps its signatures. The plan PR says `Closes` the plan issue, its task issue and #569.

**Read first:** bug #569; plan `2026-10-31-stage-3-lost-stop-fixes.md` (merged in #570: `TurnWorkers.start(..., stops_first_turn=False)` and why); `backend/src/mlops_agents/agents/messaging/inbound.py` (`SqliteInboundSource.turn_ended`), `agents/runtime/sessions.py` (`_stop_waiting`), `agents/runtime/worker.py` (`TurnWorkers.start`).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | One owner for starting the queued turns | merged code |

---

### Task 1: One owner for starting the queued turns

**Files:** `backend/src/mlops_agents/agents/runtime/sessions.py` (one public method), `backend/src/mlops_agents/agents/messaging/inbound.py` (`turn_ended` calls it), and the place `bind` is typed; tests under `backend/tests/agents/`.

**Notes:**
- **One owner.** The session manager gets one public method that starts a worker for the session's queued turns only when none is running, always with `stops_first_turn=False`. `_stop_waiting` and `SqliteInboundSource.turn_ended` both call it. The `getattr(self._sessions, "_workers", None)` read of a private field goes away. This also makes `_stop_waiting`'s `running` check reachable and testable (the lead removes the #570 debt row's clause on it at merge).
- **The lock.** `turn_ended` runs while the caller holds the session lock (`AsyncRLock`, the same task re-enters), so the method expects the session lock held and does not take it, like `TurnWorkers.start`.
- **Interface.** The inbound source does not widen `AgentSessions`: it depends on a narrow protocol with just this method (bound as today through `bind`). The exact name and shape are the owner's.
- **Why `stops_first_turn=False` is right for every redelivery:** a redelivered turn is not one the user just sent, so a Stop that arrives before the worker takes it is a Stop between two turns, and is dropped (stage 1 rule).

**Tests (stop condition)**, deterministic (events or fakes that hold a step, no sleeps):
- First, a test that fails on `main` for the reported case: a turn takes an inbound message, waits for approval, is stopped, the message is redelivered, and a second Stop arrives before the new worker takes it. The redelivered turn must run and call its model. If the case cannot be reproduced, stop and report.
- A redelivery while a worker is already running starts no second worker.
- `_stop_waiting`'s queued-turns path goes through the same method (a test pins that it starts no worker when one runs).
- The #570 tests stay green. Full backend suite, ruff, pyright; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
