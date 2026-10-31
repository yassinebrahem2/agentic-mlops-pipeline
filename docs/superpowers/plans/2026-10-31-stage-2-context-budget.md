# Stage 2: Context Budget and Session Controls Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. Four tasks, one PR for the branch, plus the early PR for tasks 1 and 2 (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, and its tests (its stop condition).

**Goal:** an agent's session can never be locked by its own context. A large file or a large tool output never reaches the model whole; old tool output stops being re-sent in full; compaction works within the provider's real per-request limit; and a person can compact an agent's session or clear its context from the session page (the user's decision of 2026-10-31, in `project_knowledge.md`). Reported by a teammate: on Groq's free tier (8,000 tokens per minute), the Orchestrator's first Read of one file filled its whole context, every later request was too large, and the session stayed locked. Read returns up to 100,000 characters today (about 25,000 tokens).

**Branch:** `plan/stage-2-context-budget`. **Milestone:** Stage 2. **Consumes:** merged code. **Produces:** nothing other plans use; the interfaces named below are this plan's own.

**Read first:** `backend/src/mlops_agents/agents/tools/read.py`, `grep.py`, `glob.py`, `bash.py` and their caps; `agents/runtime/tool_step.py` (`run`, where every tool result is built), `context.py` (`history_messages`), `compaction.py` (`compact_if_needed`), `graph.py` (~140, the summary and `compacted_through`), `model_caller.py`; `monitoring/untrusted.py` (`untrusted()` and its `<data source="...">` block); `providers/errors.py` (`ProviderErrorKind`, the classifier), `providers/models.py` and `api/routers/models.py` (`PUT /models/context-window`, the pattern task 1 follows), `frontend/src/pages/providers/ModelsTable.tsx`; `api/routers/sessions.py`; the stage 2 spec's compaction section.

**Shared with plan #612 (Stard's Gemini fix):** its task 1 changes `context.py`, `contract.py`, `model_caller.py` and `bridge.py`. Before touching `context.py` or `model_caller.py`, bring `main` in; if #612's task 1 is not merged yet, coordinate in the two plan issues rather than editing the same lines blind.

## Tasks

| Task | Title | Consumes | Starts |
|---|---|---|---|
| 1 | Each model's request limit, and "request too large" is its own error | merged code | now |
| 2 | Large output never reaches the model whole | task 1 | after task 1; **early PR for tasks 1 and 2** (people on small limits are locked out today, and the cap only helps them once their limit is set) |
| 3 | Compaction within the limit; a too-large request compacts instead of retrying | task 2 | after task 2 |
| 4 | Compact now and Clear context | task 3 | after task 3 |

---

### Task 1: Each model's request limit, and "request too large" is its own error

**Files:** `backend/src/mlops_agents/providers/models.py` (`ModelEntry`), the providers store and its new app migration (append-only, the next number), `providers/service.py`, `providers/bridge.py` (`ResolvedModel`), `providers/errors.py`; `api/routers/models.py`; new `backend/src/mlops_agents/agents/runtime/budget.py` (`request_budget_tokens`; task 2 adds the rest); `frontend/src/pages/providers/ModelsTable.tsx` (one cell) and its test; regenerated `frontend/openapi.json` and `schema.d.ts`.

**Interfaces (produced):** `ModelEntry.request_token_limit: int | None` (a person sets it; `None` means the context window); `ResolvedModel.request_token_limit`; `PUT /api/models/request-limit` (body: the model address and the limit or `null`; the same shape and errors as `PUT /models/context-window`); `budget.request_budget_tokens(model) -> int` = the smaller of the context window and the limit; `ProviderErrorKind.REQUEST_TOO_LARGE`.

**Notes:**
- Task 2's `tool_result_max_chars` and task 3's compaction read `request_budget_tokens`, nothing else.
- **"Request too large" is never retried or sent to another key.** The classifier maps it to `REQUEST_TOO_LARGE`, not `RATE_LIMIT`. Do not guess its shape: take Groq's documented response for a request above the per-minute token limit (status code and `error.code`/message), and Gemini's and OpenAI's for a request over the context or token limit, and pin each with a test built on that body. A plain per-minute 429 that a later retry can pass stays `RATE_LIMIT`.
- The Models table shows the limit and lets a person set or clear it, as it does the context window. Its help text says what it is for (a free tier's tokens-per-minute cap).

**Tests (stop condition):** the route sets and clears the limit and the listing shows it; `request_budget_tokens` takes the smaller value; the classifier maps each documented "too large" body to `REQUEST_TOO_LARGE` and a plain 429 to `RATE_LIMIT`; a `REQUEST_TOO_LARGE` error is not retried and does not rotate the key; the Models table cell sets and clears it; backend and frontend suites, ruff, pyright, lint; CI green.

---

### Task 2: Large output never reaches the model whole

**Files:** `backend/src/mlops_agents/agents/runtime/budget.py` (the one rule, beside task 1's `request_budget_tokens`); `agents/tools/read.py`; `agents/runtime/tool_step.py`; `agents/runtime/context.py`; the frontend tool-step view only if the cut notice needs styling (`frontend/src/pages/agents/ToolStep.tsx`); tests under `backend/tests/agents/`.

**Interfaces (produced):** in `budget.py`, `tool_result_max_chars(model: ResolvedModel) -> int` (how large one tool result may be for this model, from `request_budget_tokens`) and `cut_text(text: str, max_chars: int) -> str` (keeps the head, appends one notice saying how much was cut and how to narrow it); nothing else computes a size.

**Notes:**
- **One rule, one place.** `tool_result_max_chars` is a fixed share of `request_budget_tokens` in characters (owner picks the share and the chars-per-token factor, as named constants in `budget.py`, with a comment saying why). The existing per-tool constants (`MAX_CHARS` 100K in Read, the sandbox's 15K head and tail, Grep's, Glob's, `READ_HANDOFF_SECTION_CAP`, `TRACKING_HISTORY_MAX_CHARS`, `MAX_OUTPUT_CHARS`) stay as upper bounds; no tool adds a second sizing rule.
- **Read refuses a whole read that is too large,** checking the file's size before reading it (a huge file is never loaded). Read without `offset`/`limit` on a file whose content would exceed `tool_result_max_chars` returns a tool error (not a cut file) with the file's size in bytes and its line count, and says to read it in parts with `offset` and `limit` or to search it with Grep. With a range it returns at most that budget, cut by `cut_text`.
- **Every tool result is capped once, in `tool_step.run`** (it gets the turn's `ResolvedModel` the way compaction does), before it is stored and before the model sees it (so what is stored is what the model saw; the UI shows the notice). An error output is capped the same way.
- **A cut never leaves an untrusted block open.** When the cut falls inside a `<data source="...">` block made by `untrusted()`, the cut text closes the block before the notice, so nothing after it is read as data. Use `untrusted`'s own tag (imported, never retyped).
- **Older tool output is trimmed when history is rebuilt.** In `history_messages`, a tool result from a turn before the current one, longer than a small fixed size (a named constant in `budget.py`), is sent as its head plus a notice that the full output is in the session. Each tool call stays paired with its result (the call ids are unchanged). The stored messages and the UI keep the full stored content.

**Tests (stop condition):**
- First, one that fails on `main`: with a model whose request limit is 8,000 tokens, a whole-file Read of a file larger than its budget returns the error with size and line count, and the model step after it gets that error, not the file.
- A Read with a range larger than the budget is cut with the notice.
- A Bash output and a Grep output over the budget are cut once, with the notice, in what is stored and what the model gets.
- A cut inside an `untrusted()` block closes the block.
- A large tool result from an earlier turn is sent trimmed in the next turn's request, and the full content is still in the stored message; tool calls and results stay paired.
- Backend suite, ruff, pyright; CI green.

**PR:** early PR into `main` for tasks 1 and 2, from `plan/stage-2-context-budget-task2` at this task's last commit (`Closes` both task issues).

---

### Task 3: Compaction within the limit; a too-large request compacts instead of retrying

**Files:** `backend/src/mlops_agents/agents/runtime/compaction.py`, `model_caller.py` and the turn's failure path (`executor.py` or `waiting.py`, where a turn's end text is set); tests under `backend/tests/agents/runtime/`.

**Notes:**
- `compact_if_needed`'s threshold uses `request_budget_tokens`, not the context window alone.
- **The summary request itself fits the budget.** Today it sends every older turn in one request; it summarizes in pieces that each fit, carrying the summary forward, and the tool results inside them go through `cut_text`.
- **The last two turns** are kept as today; if they alone exceed the budget, their tool results are sent trimmed (task 2's history trim, applied to them too), never dropped.
- **A `REQUEST_TOO_LARGE` error compacts and retries that model step once.** If the request is still too large, the turn ends with a message saying so and naming Compact now and Clear context (task 4), not "The turn failed unexpectedly".
- **A limit too small for the agent:** when the system prompt and the tool definitions alone exceed the budget, no compaction can help. The turn ends at once with a message naming the model, its limit and the size of the fixed part, never a loop.

**Tests (stop condition):** a session over the budget compacts before the request; a summary of more history than the budget is made in pieces that each fit; a fake provider that answers `REQUEST_TOO_LARGE` once leads to one compaction and one retry that succeeds; one that keeps answering it ends the turn with the message (no loop, no key rotation); a limit smaller than the fixed part ends the turn at once with its message; existing compaction tests stay green; backend suite, ruff, pyright; CI green.

---

### Task 4: Compact now and Clear context

**Files:** `backend/src/mlops_agents/agents/runtime/sessions.py` (two public methods), the session store's summary write (`compacted_through` and the summary), `api/routers/sessions.py` (two routes), the event types in the events contract (`session.compacted`, `session.cleared`; compaction records no event today); `frontend/src/pages/agents/` (two buttons on the session page, a confirm for Clear, a divider in `MessageList.tsx`); regenerated `frontend/openapi.json` and `schema.d.ts`; tests.

**Interfaces (produced):** `POST /api/projects/{id}/agents/{agent}/session/compact` and `POST .../session/clear`, each 204, 409 `session_busy` when refused.

**Notes:**
- **Clear context** (the user's decision): the agent's context starts fresh. The summary is emptied and `compacted_through` moves to the session's last message, so the agent sees nothing before it; nothing is deleted, the history stays visible in the UI with a "Context cleared" divider and who cleared it, and the session stays the agent's one session (wakes and scheduled work keep landing in it). Recorded as `session.cleared` with the person and the id of the last message it cleared through; the UI places the divider from that event (`compacted_through` moves again at every later compaction, so it cannot mark where a Clear happened).
- **Compact now** runs compaction at once, whatever the size (task 3's compaction, so it fits the budget), when there is at least one finished turn before the last two; otherwise it is a no-op that says so. Recorded as `session.compacted`.
- **Both are refused with 409 while a turn is running, queued or waiting on an approval**, checked under the session's lock (the registry's `session_lock`), so a turn can't start halfway through.
- The Clear confirm says what it does: the agent forgets the conversation so far, the messages stay visible, and an agent working on an open incident will need to read it again.

**Tests (stop condition):** Clear: the next turn's request holds no earlier message or summary, the stored messages are unchanged, the divider shows, the event is recorded; Compact now: a session under the threshold compacts and the next request carries the summary; both 409 while a turn runs, while one is queued and while one waits on an approval; both 404 for an unknown agent and 401 signed out; the frontend: each button sends its request, Clear asks first, a 409 shows the server's message; full suites; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
