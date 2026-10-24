# Stage 2 — Events Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** The project's event stream, audit log and status board (spec §8): one owner each, stored in `project.db`, live over SSE.

**Branch:** `plan/stage-2-events`. **Consumes:** I8 to I12 (runtime-contracts). **Produces:** the `EventLog`, `AuditLog`, `StatusBoard` and `TurnObserver` implementations, and the routes `GET /events/stream` and `GET /agents/status` (overview).

**Read first:** spec §8 and §9 (the `status_board` field); the overview's I11 and hooks.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Event log, project event stream and audit log | runtime-contracts tasks 1 and 2 |
| 2 | Turn events | task 1, runtime-contracts task 3 |
| 3 | Status board | task 2 |

Task 1 gets an early PR: tasks, sandbox, approvals, github-work and messaging all record events.

---

### Task 1: Event log, project event stream and audit log

**Files:** new `backend/src/mlops_agents/events/log.py`, `events/hub.py`, `events/audit.py`, `backend/src/mlops_agents/api/routers/events.py`, the services wiring line, tests, `frontend/openapi.json` and `frontend/src/api/schema.d.ts` regenerated.

**Interfaces:** Consumes I11 and the `events` and `audit` tables. Produces `EventLog`, `AuditLog` and `GET /api/projects/{id}/events/stream`.

**Implementation notes:**
- `EventLog.record` stores the event, then publishes it to the project's hub. `seq` is the table's `seq`, so a client's position survives a restart.
- The hub keeps the last 1,000 events per project in memory. `subscribe(after_seq)` replays from the table when `after_seq` is older than the buffer. It yields `Resync` when `after_seq` is unknown, using stage 1's `Resync` type.
- The route follows the session stream's rules (`api/routers/sessions.py`): `Last-Event-ID` or `?after=`, the 15 s ping, closing on disconnect. Share its helpers by moving them to one module both routers import, never by copying them.
- `AuditLog.record` scrubs `detail` with stage 1's `credentials/scrub.py` and caps each value at 4,000 characters.
- `close_project` (stage 1) also closes the project's event hub. Add the hub to what it closes.

**Tests (stop condition):**
- Record, then subscribe with `after_seq`: replay comes in order, then live events. A position older than the buffer replays from the table, and an unknown one yields `Resync`.
- The SSE route resumes from `Last-Event-ID`.
- An audit `detail` holding a known secret and a 10,000-character value is stored scrubbed and capped.
- Full backend suite, ruff, format, pyright, `npm run gen:api` with no diff; CI green.

---

### Task 2: Turn events

**Files:** new `backend/src/mlops_agents/events/observer.py`, the services line that passes it to the session manager, tests.

**Interfaces:** Consumes task 1 and the `TurnObserver` hook. Produces the `turn.started`, `turn.waiting` and `turn.ended` events. `turn.ended`'s payload holds `status` and `final_text`, capped at 200 characters.

**Tests (stop condition):**
- A turn on the fake model records `turn.started` then `turn.ended` with its status and capped final text. A failed turn records `failed`.
- Full backend suite, checks; CI green.

---

### Task 3: Status board

**Files:** new `backend/src/mlops_agents/events/status.py`, a route in `api/routers/events.py`, `backend/src/mlops_agents/agents/runtime/agent.py` and `context.py` (the status summary in the system context, only for definitions with `status_board: true`), tests.

**Interfaces:** Consumes tasks 1 and 2, the catalog, `Tasks.open_tasks` when the tasks plan has merged (until then no task is shown), and the events table. Produces `StatusBoard` and `GET /api/projects/{id}/agents/status`.

**Implementation notes:**
- One function derives each agent's `AgentStatus`, and `snapshot` and `summary` both use it:
  - `inactive` for a definition with `chat: false`;
  - `running` while its session has a running turn;
  - `waiting_approval` while a turn waits;
  - `waiting_reply` when its last `message.sent` belongs to an exchange that is still open: no reply has come back, and no limit was reached;
  - otherwise `idle`.
- `last_finished` is the last `turn.ended` final text. `open_prs` come from `pr.opened` events whose task is still `in_review`.
- `summary` is at most about 1,500 characters: one line per agent with its state, task and PRs.
- The builder gets the summary only for a `status_board: true` agent, and the context builder adds it as its own section.

**Tests (stop condition):**
- Each state is derived from a crafted event history, and a `chat: false` definition is `inactive`.
- The Orchestrator's system message contains the summary; another agent's does not.
- The route returns every agent in catalog order.
- Full backend suite, checks; CI green.
