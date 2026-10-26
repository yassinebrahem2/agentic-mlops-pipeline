# Stage 2 — Frontend Agents Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** The agents page works with seven agents (spec §10):
- a list of agents with live status;
- approval cards in the chat;
- the task bar;
- the bypass warning;
- messages from other agents labeled as such.

**Branch:** `plan/stage-2-frontend-agents`. **Consumes:** I9 and I11 (generated types); the routes `GET /events/stream`, `GET /agents/status`, `POST /approvals/{id}/decision`, `GET /settings/permissions` and `POST /tasks/{id}/discard`; chat-layout merged. **Produces:** nothing other plans use.

**Read first:** spec §5 (approvals in the session) and §10; the overview's routes table; the stage 2 overview's global constraints.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | The agents list with live status | runtime-contracts task 1, chat-layout merged; merging: events tasks 1 and 3 |
| 2 | Approval cards | task 1; merging: approvals plan |
| 3 | Task bar, bypass warning and agent labels | task 2; merging: tasks task 5, approvals task 1 |
| 4 | The approval in the end-to-end flow | task 3, agent-definitions merged |

Tasks 1 and 2 get early PRs, from `plan/stage-2-frontend-agents-task1` and `-task2`: frontend-inbox uses `lib/useProjectEvents.ts` and `ApprovalCard`.

---

### Task 1: The agents list with live status

**Files:** `frontend/src/pages/agents/AgentsPage.tsx`, new `pages/agents/AgentList.tsx`, new `frontend/src/lib/useProjectEvents.ts` (the project event stream, reusing the session stream's reconnect logic, moved into a shared module rather than copied), new `lib/useAgentStatus.ts`, tests.

**Implementation notes:**
- A column on the left lists the chat agents (and the inactive one, greyed, with its status text). Each shows its state as a dot and words: idle, running, waiting for approval, waiting for a reply. Agents with a `waiting_approval` state get a badge.
- Selecting an agent opens its session. This replaces chat-layout's agent switcher. On narrow screens the list becomes a select (`components/ui/select.tsx`).
- `GET /agents/status` loads the list. Each project event refetches it, debounced.

**Tests (stop condition):** the list renders every agent with its state; an event refetches; selecting changes the session; `npm test` (nothing else heavy running), lint, format check, build; CI green.

---

### Task 2: Approval cards

**Files:** new `frontend/src/pages/agents/ApprovalCard.tsx`, `pages/agents/MessageView.tsx` (renders the `approval` part next to its tool call), `pages/agents/session/applyEvent.ts` (`turn.waiting`), tests.

**Implementation notes:**
- The card shows the tool and the preview:
  - kind `diff`: a unified diff view, added and removed lines colored, monospaced, scrolling;
  - `command`: a code block;
  - `text`: preformatted text.
- Pending cards show Approve, and Reject with an optional note. Decided ones show their status (approved, rejected with the note, cancelled, expired).
- A waiting turn shows "Waiting for your approval" in place of the typing indicator.
- Deciding calls the route. The card updates from `message.updated`, never optimistically.

**Tests (stop condition):** each preview kind renders; Approve and Reject (with a note) call the route; a decided status renders; `turn.waiting` shows the waiting line; checks; CI green.

---

### Task 3: Task bar, bypass warning and agent labels

**Files:** new `pages/agents/TaskBar.tsx`, `pages/agents/MessageView.tsx` (user-kind messages with `from_agent`: "From <author_name>" when `from_agent` is another agent, and `author_name` alone when it is the session's own agent, as for wake-ups and review results), `AgentsPage.tsx` (the warning bar), tests.

**Implementation notes:**
- The task bar sits above the composer when `current_task` is set. It shows the title, the branch, and the PR link or "local review". "Discard task" opens a confirmation dialog and calls the route; a 409 shows its message.
- The bypass bar shows when `GET /settings/permissions` returns `bypass`. It says that agents act without asking.

**Tests (stop condition):** the bar renders with and without a PR; discard calls the route after confirming; the 409 message shows; labels render for agent messages and wake-ups; the bypass bar shows only on bypass; checks; CI green.

**Review fixes (added in the review of #238):** task 3 also fixes these in `ApprovalCard.tsx` and `MessageView.tsx`:
- after a successful decision the buttons stay disabled until `message.updated` arrives (reset only on error), and the double-submit guard is a ref, so two clicks in one tick send one decision; a test clicks twice;
- the note's placeholder says it goes with either decision;
- the no-op wrapper `div` around `ToolStep` goes.

---

### Task 4: The approval in the end-to-end flow

**Files:** `frontend/e2e/flow.spec.ts` (a new step after the stage 1 flow), `backend/tests/fakes/fake_openai.py` (a model `fake-write` that calls StartTask in one step, then Write on `NOTES.md` in the next, then answers, and answers the PR reminder's extra request too: the e2e project is local with no remote), tests.

**Implementation notes:** the new e2e step:
1. Set the project's level to `ask` (it is the default).
2. Ask the Orchestrator on `fake-write`.
3. An approval card for StartTask appears; approve it.
4. A card with the `NOTES.md` diff appears; approve it.
5. The turn ends, and the task bar shows the task.

This uses only visible labels, as stage 1's e2e does.

**Tests (stop condition):** `npm run build && npm run e2e` passes; CI green, E2E included.
