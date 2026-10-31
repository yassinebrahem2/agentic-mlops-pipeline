# Stage 3: Lost Stop Fixes Implementation Plan

> **For agentic workers:** Implement this plan as `docs/implementing-plan-tasks.md` says. One task, one PR for the branch, no early PR (`AGENTS.md` rule 5). The task lists its files, tests (its stop condition) and checks.

**Goal:** a Stop is never lost and never hits the wrong turn: bug #446 (windows A and C, a queued message stranded after a Stop, two quick Stops). The user's call of 2026-10-31: fix it now.

**Branch:** `plan/stage-3-lost-stop-fixes`. The plan PR says `Closes` the plan issue, its task issue and #446. **Consumes:** merged code. **Produces:** nothing other plans use; every public interface keeps its signature (`AgentSessions.stop`, `stop_turn`, `resume_turn`).

**Read first:** bug #446 (its body and both comments); Decision #445 (window B, fixed in #393, whose test must stay green); `backend/src/mlops_agents/agents/runtime/sessions.py` (`resume_turn`, `_stop`) and `worker.py` (`TurnWorkers.start`, `resume`, `stop`, `_loop`, `_leave`).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | A Stop is decided under the session lock | merged code |

---

### Task 1: A Stop is decided under the session lock

**Files:** `backend/src/mlops_agents/agents/runtime/sessions.py` (`_stop`), `backend/src/mlops_agents/agents/runtime/worker.py` (`_loop`, and what a worker started by Stop carries); tests under `backend/tests/agents/runtime/`, and `backend/tests/agents/test_approval_stop_races.py` (Decision #562).

**Why it is lost today:** `_stop` calls `workers.stop` before it takes the session lock and looks for a waiting turn after. `resume_turn` starts the resume's worker under the lock, and the worker takes the waiting turn under the lock (`take_waiting_turn`, the one check that it still waits). A Stop whose `workers.stop` runs before that worker exists stops nothing. Then it either finds the turn already `running` (window A) or finds it `waiting` and awaits the worker that runs it to `done` (window C).

**Notes:**
- **Keep Stop instant, then decide under the lock** (Decision #566). `TurnWorkers` gets one marker, "this worker's turn went waiting", set in the same callback that adds the key to `_turn_over` (`_WaitAwareSink`), and cleared wherever `_turn_over` is cleared (a turn taken, `_leave`, `forget`), so a new worker never inherits it. `TurnWorkers.stop` classifies in one synchronous call and returns the outcome; `_stop` branches on it and reads no private field:
  - **landed:** the worker is running a turn (the turn task is cancelled, as today), or it is before its first look (the Stop goes to the turn it takes, `_stop_next`). Done.
  - **dropped:** the worker is between two turns (in `_turn_over` without the went-waiting marker). A Stop between two turns is dropped (stage 1 rule). Done.
  - **waiting:** the worker's turn went waiting and it is only finishing its loop. Do not cancel anything (cancelling a turn while it reports its wait is what leaves the orphan checkpoint, the #531 row in `docs/known-debt.md`).
  - **no worker.**
  On **waiting** or **no worker**, `_stop` decides in a loop under the session lock:
  1. A worker is running now (`resume_turn` may have started one): classify it again; landed or dropped ends here.
  2. It is the went-waiting worker: release the lock, await that worker (never while holding the lock: the worker needs it), and go back to step 1. `resume_turn` also waits for that worker and may take the lock first and start the resume's worker.
  3. A matching turn waits: stop it (`stop_waiting_turn`, as today), then start a worker for the queued turns only if none is running.
  4. Otherwise there is nothing to stop.
  Because the resume's worker is started and takes the turn under the same lock, a Stop now lands either before the start (the turn is stopped while waiting, so `resume_turn` and the worker find it not waiting) or after it (the worker is stopped).
- **`stop_turn(turn_id)` stops only that turn, and is not lost either:** in step 1 it calls `workers.stop` only when the worker is on exactly this `turn_id`, either its current turn, or (a resume's worker before its first look) the turn it resumes. `resume()` stores that turn ID, and `TurnWorkers` exposes one accessor for "the turn this worker is on or about to resume". A worker on another turn, a queued one say, is left alone. Steps 2 and 3 apply to that `turn_id` as for `stop`.
- **A worker that Stop starts for the queued turns** is started only when none is running (`TurnWorkers.start`'s precondition). A second Stop that arrives before it takes its first turn is a Stop between two turns, so it is dropped (stage 1 rule) and must not stop the queued turn: such a worker does not take Stops for its first turn the way a worker started by a Send does.
- **A resume's worker whose take lost to a Stop** goes on to the session's queued turns while it is still the registered worker, instead of leaving. Otherwise a Send that queued behind the Stop, and saw this worker running, is stranded `queued` with no worker.
- `sessions.py` is about 414 lines; the ~400 guideline is not a blocker, but if `_stop` grows, moving its body next to `stop_waiting_turn` in `waiting.py` is fine.

**Tests (stop condition)**, deterministic (events or fakes that hold a step, no sleeps), each new test failing on `main` before the fix:
- Window A, both orders: approve, then Stop, with Stop's locked decision running once before `resume_turn` starts the worker and once after. Both end `stopped`, and the model is not called again. The same for `stop_turn` on that turn.
- Window C: Stop when the resume's worker exists but has not taken the turn. The turn ends `stopped` and the model is not called.
- Window B (#393's test) still passes.
- A message sent right after a Stop of a waiting turn runs: it doesn't stay `queued`.
- A Stop that lands while `turn.waiting` is being reported (hold the sink callback with an event): the turn ends `stopped`, the Stop is not dropped, and no worker is cancelled mid-report.
- The three tests in `test_sessions_stop.py` (a Stop before the worker takes the queued turn, one that finds no more turns, one that stops neither) stay unchanged: they pin "a Stop between two turns is dropped".
- Two Stops in quick succession, the second arriving before the worker that the first Stop started takes its first turn: the queued turn runs and its model is called.
- `stop_turn` for a waiting turn while the worker runs another turn: the other turn is not stopped.
- A Stop of a running turn, of a waiting turn, and with nothing to stop behave as today (the existing tests stay green).
- `test_approval_stop_races.py` (Decision #562): its three tests that wait for Stop's unlocked read of `waiting_turn` (the read this fix removes) move only their hold points and keep every assertion. `..._is_kept` is re-targeted at the worker's take guard, which stays as defence in depth (approval cancellation and the restart repair also store `stopped`): `TurnWorkers.resume` on a turn already stored `stopped`, not through `_stop`, takes nothing and the turn stays `stopped`; its docstring says so, and drops the line that windows A and C are not covered.
- The reviewer proves the re-timed tests still race: with the unlocked decision restored, the new window A (both orders) and window C tests fail; with the `take_waiting_turn` guard disabled, the re-targeted `..._is_kept` fails; each of the three modified tests changes only its hold points.
- No production path holds the session lock while it awaits `stop`, `stop_turn` or anything Stop waits on, from another task (`AsyncRLock` re-enters only for the same task); the PR says what was checked.
- Full backend suite, ruff, pyright; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
