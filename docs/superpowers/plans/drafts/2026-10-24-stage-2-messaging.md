# Stage 2 — Messaging Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** Agents talk to each other (spec §7):
- SendMessage within limits, delivered to the recipient mid-turn;
- ListAgents;
- wake-ups an agent schedules for itself.

**Branch:** `plan/stage-2-messaging`. **Consumes:** I8 to I12; events tasks 1 and 3. **Produces:** the `MessageBus` and `InboundSource` implementations, the tools SendMessage, ListAgents and ScheduleWakeup, and `GET` and `PUT /settings/messaging`.

**Read first:** spec §7, all of it.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | SendMessage, exchanges and limits | runtime-contracts tasks 1 to 3, events task 1 |
| 2 | Mid-turn delivery | task 1 |
| 3 | ListAgents | task 1, events task 3 |
| 4 | Wake-ups | task 2 |

---

### Task 1: SendMessage, exchanges and limits

**Files:** new `backend/src/mlops_agents/agents/messaging/bus.py`, `agents/messaging/settings.py`, `agents/tools/send_message.py`, `api/routers/messaging.py`, `agents/tools/__init__.py` (its line), the services lines, tests, regenerated API types.

**Interfaces:** Consumes I9's `post`, I12's `MessageBus`, `EventLog`, the catalog. Produces:
- `MessageBus`;
- the tool SendMessage (`message`);
- the project settings `message_max_hops` (default 6) and `message_max_tokens` (default 300,000);
- `GET` and `PUT /settings/messaging`.

**Implementation notes:**
- `send`:
  - checks `to` against the sender's `can_message`, so an unknown or forbidden agent is a tool error;
  - finds the exchange: if the sender's turn read a message from `to` (`read_by_turn` = the sender's turn, `from_agent` = `to`), it continues that exchange with that message's hop plus one; otherwise a new exchange (`xch`) at hop 1;
  - checks the limits (hops; tokens of the `llm_calls` of the turns that read the exchange's messages) and raises `MessageLimitReached` past them;
  - posts to the recipient (`author_name` = the sender's display name);
  - records `message.sent`, with sender, recipient, exchange and hop.
- The tool answers "Sent to <display name>."

**Tests (stop condition):**
- Send while idle: the recipient's turn runs, and its context shows "Message from <sender>".
- A reply continues the exchange at hop 2.
- Hop 7 is refused, and so is a token budget passed.
- A forbidden recipient is refused.
- The settings round-trip.
- Full suite, checks; CI green.

---

### Task 2: Mid-turn delivery

**Files:** new `agents/messaging/inbound.py`, a new `agents/runtime/store/inbound.py` (the queries it needs, composed into `SessionStore`), the services line (`NoInbound` replaced and deleted), tests.

**Interfaces:** Consumes task 1 and the `InboundSource` hook. Produces the real `InboundSource`.

**Implementation notes:**
- `take` runs under the session lock. It selects the session's queued messages with `from_agent` set, in `seq` order. It marks each `complete` with `read_by_turn` set to the current turn, deletes the queued turn that each one triggered, and publishes `message.updated`. It returns them as `HumanMessage`s with stage 1's "Message from <name>:" prefix, which the context builder already uses.
- User messages (no `from_agent`) are never taken mid-turn.
- Whatever is not taken keeps its queued turn, so the worker starts the next turn from the earliest waiting input by `seq`: queued user messages and undelivered agent messages alike.

**Tests (stop condition):**
- A message posted while the recipient runs a slow tool (use the hold gate) appears in its next model request, after that step's tool results, and starts no extra turn.
- A message posted after the last model step starts the next turn.
- A queued user message is not taken mid-turn.
- A waiting turn gets the message at its first step after resuming.
- Full suite, checks; CI green.

---

### Task 3: ListAgents

**Files:** new `agents/tools/list_agents.py`, `agents/tools/__init__.py` (its line), tests.

**Interfaces:** Consumes `StatusBoard.snapshot` and the catalog. Produces the tool ListAgents (`read`).

**Implementation notes:** for each agent it returns:
- name, display name and description;
- whether it chats;
- state, task and open PRs;
- whether the caller may message it.

It is plain text, one block per agent.

**Tests (stop condition):** the output for a crafted status board; `may message` follows `can_message`; full suite, checks; CI green.

---

### Task 4: Wake-ups

**Files:** new `agents/messaging/wakeups.py` (store and the one timer), `agents/tools/schedule_wakeup.py`, `agents/tools/__init__.py` (its line), the services lines (start the timer; stop it on close), tests.

**Interfaces:** Consumes task 2. Produces the tool ScheduleWakeup (`message`, only for definitions with `schedules: true`, so any other caller gets a tool error) and the wake-up timer.

**Implementation notes:**
- Limits:
  - delay from 60 s to 7 days;
  - at most 5 pending per agent;
  - at most 24 fired per agent per rolling day; past that the tool refuses.
- One asyncio task sleeps until the earliest due wake-up, and wakes early when a new one is scheduled sooner. Due wake-ups are marked `fired` and posted (`author_name` "Scheduled wake-up", `from_agent` the agent itself, text "Scheduled wake-up: <note>"). They are recorded as `wakeup.scheduled` when stored.
- At startup, wake-ups that fell due while the app was down fire at once.
- The clock is injected, so tests run without sleeping.

**Tests (stop condition), with a fake clock:**
- A wake-up fires and starts a turn whose context has the note.
- The limits are enforced.
- One due during downtime fires at startup.
- An agent without `schedules` is refused.
- Full suite, checks; CI green.
