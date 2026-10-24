# Stage 2 — Approvals Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** The permission policy is enforced in code (spec §5):
- every tool call is allowed, denied or held for the user's approval;
- a held turn waits, survives a restart, and resumes when the user decides;
- everything is audited.

**Branch:** `plan/stage-2-approvals`. **Consumes:** I8 to I12, events task 1. **Produces:** the `Gate` and `Approvals` implementations, the routes `GET /approvals`, `POST /approvals/{id}/decision`, `GET` and `PUT /settings/permissions`, and the waiting turn.

**Read first:** spec §5, all of it, and the stage 1 spec §5 (Turns, Recovery). This plan owns the executor and session-manager changes for waiting and resuming (overview, Runtime sequencing).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | The permission level | runtime-contracts tasks 1 and 2 |
| 2 | The gate | task 1, events task 1 (tested in a one-node test graph with a checkpointer; `interrupt()` needs only a graph context) |
| 3 | Waiting turns | task 2, runtime-contracts tasks 3 and 4 merged |
| 4 | Deciding and resuming | task 3 |
| 5 | Expiry and Stop | task 4 |

The real gate replaces `AllowAllGate` in task 4, once a held turn can resume. No early PR.

---

### Task 1: The permission level

**Files:** new `backend/src/mlops_agents/agents/approvals/settings.py`, `backend/src/mlops_agents/api/routers/approvals.py` (the permissions routes), tests, regenerated API types.

**Interfaces:** Consumes I10. Produces the project setting `permission_level` (default `ask`), and `GET` and `PUT /settings/permissions`.

**Tests (stop condition):** default `ask`; every level round-trips; an unknown level gets a 422; full suite, checks, `npm run gen:api` with no diff; CI green.

---

### Task 2: The gate

**Files:** new `backend/src/mlops_agents/agents/approvals/gate.py`, `agents/approvals/store.py`, tests.

**Interfaces:** Consumes task 1, I10, I12's `Gate`, `AuditLog`, `EventLog`. Produces the `Gate` implementation (`decide` and `wait`), not wired yet.

**Implementation notes:**
- `decide` runs once per step and never pauses. For each `GateCall` it:
  - looks up the level;
  - gets `policy.decide(tool.risk, level)`;
  - writes one audit record (`allowed`, `denied` or `asked`).
- A deny's message names the level and the risk class. For `suggest` with `write_code`, it tells the agent to propose the change as a diff in its answer.
- An ask creates an approval record (with `tool.preview(ctx, args)`) and records `approval.requested`. Its `GateDecision` carries the approval id and preview, so the graph adds the `ApprovalPart`.
- `wait` reads the step's approvals from the store. If any is pending, it calls LangGraph's `interrupt()`. Once none is pending, it returns `approved`, or `rejected` with the message "The user rejected this action" plus the note.
- `wait` has no side effects, because LangGraph re-runs it on resume.

**Tests (stop condition):**
- For each level, a step mixing read, write, run and github calls gets the table's decisions and one audit record each.
- `wait` with a pending approval raises LangGraph's interrupt. With decided ones, it returns them, the note included.
- Full suite, checks; CI green.

---

### Task 3: Waiting turns

**Files:** new `backend/src/mlops_agents/agents/runtime/waiting.py`, `agents/runtime/executor.py`, `agents/runtime/worker.py`, the store package's turns module, tests.

**Interfaces:** Consumes task 2 and I9 (`waiting`, `TurnWaiting`). Produces the waiting turn.

**Implementation notes:**
- When `ainvoke` returns with an interrupt, the executor (through `waiting.py`):
  - marks the agent message `waiting` and the turn `waiting`;
  - publishes `TurnWaiting` and calls the observers' `turn_waiting`;
  - keeps the turn's checkpoint thread, and still closes its connection.
- The executor returns a distinct waiting outcome, separate from `TurnEnd`. The worker then publishes no `turn.ended`, sets the turn `waiting`, and exits. The waiting turn has no live worker.
- The one "busy" function (runtime-contracts task 3) counts a `waiting` turn, so `send`, `post` and `get` queue behind it and never start another turn. Only a resume continues that session; after it ends, the worker picks up the queue as usual.
- Recovery at startup leaves waiting turns and their `waiting` messages untouched. Stage 1 already leaves waiting turns; check the message side.

**Tests (stop condition):** a test gate that asks: the turn ends up `waiting`, the message `waiting`, the thread kept and the connection closed; a second message queues; after rebuilding the session manager (a restart) the turn is still `waiting`; full suite, checks; CI green on Linux and Windows.

---

### Task 4: Deciding and resuming

**Files:** new `agents/approvals/service.py` (`Approvals`), `api/routers/approvals.py` (`GET /approvals`, `POST .../decision`), `agents/runtime/waiting.py`, `executor.py` and `worker.py` (resume), the services lines (the real gate replaces `AllowAllGate`, which is deleted), tests.

**Interfaces:** Consumes task 3. Produces `Approvals`, the approvals routes, and resuming.

**Implementation notes:**
- `decide` sets the status, note and time, updates the `ApprovalPart` through the session manager's `update_message_parts` (runtime-contracts task 3), writes the audit record (`approved` or `rejected`, actor `user`), and records `approval.decided`.
- When no approval of that turn is pending any more, the session manager resumes the turn in its worker:
  - the executor opens a new checkpointer connection;
  - it loads the waiting message's id and parts as the turn's current message and parts;
  - it marks both `streaming` and `running` again;
  - it runs `graph.ainvoke(Command(resume=True), config)`.
- A decision on an approval that is not pending gets a 409.
- With the real gate wired, every existing test runs at the default level `ask`. Test tools of risk `read` are unaffected; any test turn that calls a non-`read` tool sets its project to `autonomous` in its setup. Every stage 1 test passes.

**Tests (stop condition):**
- Approve: the tool runs, the turn continues and ends `done`, and the message keeps its earlier parts.
- Reject with a note: the model receives the rejection and note.
- Two asks in one step: the turn resumes only after both are decided.
- Approve after rebuilding the session manager: the turn resumes from the stored checkpoint, keeping its earlier parts.
- Resuming creates no second approval or audit record.
- Full suite, checks; CI green on Linux and Windows.

---

### Task 5: Expiry and Stop

**Files:** `agents/approvals/service.py`, `agents/runtime/waiting.py` (Stop on a waiting turn, called by the session manager's `stop`), the store package's recovery, tests.

**Interfaces:** Consumes task 4. Produces the `expired` and `cancelled` rules.

**Implementation notes:**
- At project recovery, a pending approval whose turn is not `waiting` becomes `expired`. Its part is updated through `update_message_parts` and it is audited `expired`.
- Stop on a waiting turn marks its pending approvals `cancelled` (audited `cancelled`), ends the turn `stopped`, deletes its checkpoint thread, and marks the message `stopped`. The next queued message then runs.

**Tests (stop condition):** a turn left `running` with a pending approval becomes `interrupted` and its approval `expired`; Stop on a waiting turn cancels its approvals and the queue moves on; full suite, checks; CI green.
