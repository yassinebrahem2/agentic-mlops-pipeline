> **Draft.** Missing: the lead's advisor review.

# Stage 3 — Rate Limits Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works. Shared context: `docs/superpowers/plans/2026-10-28-stage-3-overview.md`.

**Goal:** turns survive rate limits (spec §2):
- a 429 moves to the next key at once; a per-minute limit never ends a turn;
- key state is kept per provider, key and model, shared by the whole app;
- when every key is exhausted the turn pauses on a `stall` approval (card with Try again and Stop) instead of failing, and no work is lost.

**Branch:** `plan/stage-3-rate-limits`. **Consumes:** task 1 none; tasks 2 to 4 I13 (contracts task 2: `Approvals.create_stall`, the `stall` kind, `kind` and `step` on `ApprovalPart`, `turn.stalled`, `turn.resumed`, `error_kind`, the card dispatcher and `ApprovalControls`). **Produces:** `KeyFailover`, `AllKeysExhausted`, `RateLimited`, `StreamedTextFailure` (I21) from task 1 (early PR); the stall; the exhausted-key cooldown setting.

**Read first:** spec §2, §3 (the embedder never stalls) and §12 (stalled-turn card, cooldown field); overview I13, I14 (`EmbedderUnavailable`), I21 and "Files shared"; the stage 2 approvals plan (`2026-10-24-stage-2-approvals.md`) for how a turn waits and resumes. Existing code: `providers/errors.py`, `providers/bridge.py` (`ResolvedModel`), `agents/runtime/model_caller.py`, `agents/runtime/graph.py`, `agents/runtime/graph_run.py`, `agents/runtime/waiting.py`, `agents/approvals/{store,service}.py`. The reference engine (`D:\SCRIPTS\Multimodal RAG Agent\src\mmrag\providers\retry_engine.py`) is for the concept only: it waits on one key and swaps after a streak, which is not the user's rule below. Never copy it.

**Fake clock:** every time rule is tested with `FakeClock` (`backend/tests/providers/fake_clock.py`, created by task 1): `monotonic()`, `wall()` and `async sleep(s)` advance both clocks and record `s`, so no test waits in real time.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | `KeyFailover` and the error classification | none |
| 2 | The cooldown setting and the model caller on `KeyFailover` | task 1 |
| 3 | The stall in the turn graph | task 2, contracts task 2 |
| 4 | The stalled-turn card, Try again on a failed turn, and the cooldown field | task 3 |

Task 1 gets an early PR from `plan/stage-3-rate-limits-task1`: embedders consumes `KeyFailover`.

---

### Task 1: `KeyFailover` and the error classification

**Files:** new `backend/src/mlops_agents/providers/failover.py`, `providers/errors.py` (classification), `providers/models.py` (one field, `Provider.exhausted_cooldown_minutes`, default from `DEFAULT_EXHAUSTED_COOLDOWN_MINUTES = 15`, which lives in `failover.py` as its one source), new `backend/tests/providers/fake_clock.py`, new `tests/providers/test_failover.py` (split a second file past 400 lines), `tests/providers/test_errors.py`.

**Interfaces:** Consumes `Provider`, `KeyHint`, `ProviderCallError`, `ProviderErrorKind`, `classify`. Produces I21 exactly as the overview has it:

```python
KeyStatus = Literal["ok", "limited", "exhausted_daily", "billing", "rejected"]
class KeyState(BaseModel): key_index: int; status: KeyStatus; since: datetime | None
class AllKeysExhausted(Exception): keys: list[KeyState]
class RateLimited(Exception): retry_after_s: float
class StreamedTextFailure(Exception): cause: BaseException
class KeyFailover:
    async def call[T](self, provider: Provider, model: str, attempt: Callable[[int | None], Awaitable[T]], *, wait: bool = True) -> T: ...
```

Additions of this plan (not in I21; see Interface gaps): `KeyFailover.__init__(self, clock: Clock | None = None)` with `Clock` a Protocol (`monotonic() -> float`, `wall() -> datetime`, `async sleep(seconds)`; `None` is the system clock); `KeyFailover.states(provider: Provider, model: str) -> list[KeyState]` (every key's status now); `KeyFailover.clear(provider_id: str) -> None` (every key and model of the provider); `describe_keys(provider: Provider, keys: list[KeyState]) -> str` (the one per-key explanation, used by the failure text and the stall card).

**Implementation notes:**
- `attempt(key_index)` makes ONE call and raises the provider's raw exception; `KeyFailover` classifies it with `classify(exc)` (kind and `retry_after` only; the message is not used, so no key value is needed). Whatever the failover does not handle it re-raises as the raw exception, unchanged, so the caller classifies it again with the key values to scrub. `asyncio.CancelledError` and any other non-provider exception pass through. A provider with no keys (`provider.keys` empty) gets `attempt(None)` once, with no failover and no state.
- **State** is one dict keyed `(provider_id, key_index, model)`; each entry: `last_limited`, `retry_after` (monotonic), `exhausted` (`exhausted_daily | billing | rejected | None`), `since` (wall time of the mark or the limit), `probe_at` (monotonic). Plus a cursor per `(provider_id, model)`: the index to try first.
- **Concurrency:** every read-then-write of the state is one synchronous block with no `await` inside, so no lock is needed and none is held across `attempt` or `sleep`. Calls (the four kickoff audits) share the state; two calls limited on one key within a minute never exhaust it between them.
- **Loop of `call`:**
  1. Pick (synchronously). Probes first: an exhausted key whose `probe_at <= now` (taking it sets `probe_at = now + cooldown`, so one call probes at a time; the cooldown is `provider.exhausted_cooldown_minutes`). Then ready keys: not exhausted, and never limited or past its clear time (`last_limited + min(retry_after or 60, 60)`), in index order from the cursor.
  2. Nothing pickable and no non-exhausted key: raise `AllKeysExhausted(states)`. Some key limited: with `wait=False` raise `RateLimited(retry_after_s=earliest clear time - now)`; with `wait=True`, `await clock.sleep(that)` (never more than 60 s; the earliest probe time counts too) and pick again.
  3. `attempt(index)`. Success: clear the key's `last_limited` and `exhausted`, set the cursor to it, return.
  4. Failure, by `classify(exc).kind`:
     - `RATE_LIMIT`: a probe attempt stays exhausted (`probe_at` already pushed); otherwise, when the key's `last_limited` is at least 60 s old (`>=`, tested at 59.9 and 60.0) it becomes `exhausted_daily`; else record `last_limited = now` and `retry_after`. The cursor moves to the next index and the loop continues at once with no sleep and no counter: rate limits never spend a retry budget.
     - `QUOTA` (billing): exhausted `billing` at once. `AUTH`: exhausted `rejected` at once. Next key.
     - `TRANSIENT`, `UNREACHABLE`: retry the same key after `TRANSIENT_BACKOFFS = (1.0, 2.0)` seconds (moved here from the model caller; one counter per call), then re-raise the raw exception. A different key would not fix it.
     - any other kind: re-raise at once.
  5. `StreamedTextFailure(cause)`: apply step 4's state change for `cause`, then re-raise the `StreamedTextFailure` without trying another key.
- Key add or remove shifts indices, so task 2 calls `clear(provider_id)` on both.
- Known consequence of the user's rule, kept as specified: a key limited once, idle over a minute and limited again before any success is marked exhausted; it returns through the cooldown probe.
- `describe_keys` lines read like "Key ...abcd: daily limit reached (since 14:03)", "billing: out of credit", "rejected: the provider refused this key", "limited: wait ends in 20 s", using the hints in `provider.keys`; no key value ever appears.
- Classification in `errors.py` (`_kind`): every HTTP 429 is `RATE_LIMIT`, whatever its wording ("quota exceeded" included). `QUOTA` only for HTTP 402 or the code `insufficient_quota` (attribute, body or nested `error.code`; a 429 carrying it is `QUOTA`). The openai SDK raises a plain `APIStatusError` for 402, so test `status_code == 402` before the generic branch. `AUTH` unchanged. Remove the `"quota" in str(e)` test. `Retry-After` stays numeric seconds (an HTTP date gives `None`).

**Tests (stop condition):** all with `FakeClock`, an `attempt` script and no network:
- a 429 on key 0 moves to key 1 with no sleep recorded; a success clears the limit time;
- a key limited again after 59.9 s is not exhausted, after 60.0 s it is `exhausted_daily` (a single key then raises `AllKeysExhausted`);
- every key limited under a minute ago: exactly one sleep, until the earliest clear time (`Retry-After` 5 s gives 5, none gives 60, 3600 gives 60), then that key is tried; with `wait=False` the same state raises `RateLimited` with that delay and records no sleep;
- a success between two limits resets the clock; ten 429s then a success works (no budget);
- state is per model and per provider;
- exhausted keys are skipped; after the cooldown exactly one probe, concurrent calls do not probe together, a failed probe stays exhausted another cooldown, a success clears it; `clear(provider_id)` clears all;
- billing (`insufficient_quota` and 402) and rejected (401, 403) exhaust at once with their status; every key exhausted raises `AllKeysExhausted` with each key's status and `since`; `states` reports `ok` and `limited` too;
- transient and unreachable retry with the two backoffs then re-raise the raw exception; bad request, bad generation and unknown re-raise at once;
- `StreamedTextFailure` records the limit, never moves to another key, re-raises; a provider with no keys gets `attempt(None)` once;
- two concurrent calls on shared state; a cancel during a sleep propagates and leaves the state valid; `describe_keys` output holds no key value;
- errors: 429 worded "quota exceeded" is `RATE_LIMIT`; 429 `insufficient_quota` and HTTP 402 are `QUOTA`; 401 and 403 `AUTH`; 5xx `TRANSIENT`; the `Retry-After` cases;
- `uv run ruff check`, `uv run ruff format`, `uv run pyright`, `uv run pytest`; CI green (both OS jobs).

**PR:** early PR into `main`, `Closes #<task issue>`, from `plan/stage-3-rate-limits-task1`.

---

### Task 2: The cooldown setting and the model caller on `KeyFailover`

**Files:** `agents/runtime/model_caller.py`, `agents/runtime/sessions.py` (the `ModelCaller(...)` construction line), `providers/service.py` and `api/routers/providers.py` (the lines named below), `api/services.py` (its own lines), `tests/fakes/fake_openai.py` (new functions only), `tests/agents/runtime/test_model_caller.py` (updated), new `tests/agents/runtime/test_model_caller_failover.py`, `tests/providers/test_service.py`, provider route tests, `frontend/openapi.json` and `frontend/src/api/schema.d.ts` (regenerated).

**Interfaces:** Consumes task 1, `ResolvedModel`, `ProvidersService`. Produces `ModelCaller(providers, http, failover)`; `ModelKeysExhausted(TurnFailed)` (carries `provider`, `model`, `keys`, and `error_kind = "keys_exhausted"`); `PUT /api/providers/{name}/settings` with `{exhausted_cooldown_minutes: int}` (1 to 1440); `Provider.exhausted_cooldown_minutes` filled in.

**Implementation notes:**
- `build_services` creates the one `KeyFailover` and hands it to `ProvidersService` (for `clear`) and to `ModelCaller`.
- The cooldown is stored in the existing app `settings` key-value table, key `provider.<id>.exhausted_cooldown_minutes` (no migration); `get_provider` and `list_providers` fill it, defaulting to `DEFAULT_EXHAUSTED_COOLDOWN_MINUTES`; `delete_provider` removes it; `add_key`, `remove_key` and `delete_provider` call `failover.clear(provider.id)`.
- `ModelCaller.call` keeps: building the client, streaming, `on_text`, usage, tool-call indexes and the `BAD_GENERATION` corrective retry (`MAX_REJECTED_RETRIES`; an outer loop around `failover.call` that appends the corrective message). It loses `MAX_ATTEMPTS`, `TRANSIENT_BACKOFFS`, key rotation and the `sleep` argument (existing tests move to `FakeClock`).
- `attempt(key_index)` builds the model with that key (`None` means the keyless build) and lets the raw exception out. Once any text has streamed in the call, it wraps whatever a provider call raised in `StreamedTextFailure(cause)`. The `on_text` callback wrapper (`_OnTextCallbackError`) is unchanged and never reaches the failover as a provider error.
- Endings, one text from `describe_keys`:
  - `AllKeysExhausted` before any text: raise `ModelKeysExhausted` (a `TurnFailed`, so every current caller, compaction included, fails as before until task 3 catches it in the model node), text `"<provider>: no usable key for <model>. " + describe_keys(...)`.
  - `StreamedTextFailure`: classify `cause` with the key values, raise `TurnFailed` (with `error_kind = "keys_exhausted"` when the cause was a rate limit, billing or auth) whose text is the cause's message plus `describe_keys(provider, failover.states(...))`.
  - When every key is `billing` or `rejected` (none daily): also `providers.mark_needs_attention(provider_id, message)`, as today.
  - Other provider errors: classify the raw exception with the key values and raise `TurnFailed(error.message)` as today.
- Fake OpenAI server, new functions of their own: `FakeState.script_for_key(key, outcomes)`, consumed per request by the `Authorization` bearer key. Outcomes: `"ok"`, `("429", retry_after | None)`, `"429-quota-wording"` (message "You exceeded your current quota", code `rate_limit_exceeded`), `"402"`, `"insufficient_quota"` (429), `"401"`. An empty script keeps the present behavior.
- `npm run gen:api` after the route exists; commit the result.

**Tests (stop condition):**
- fake server, two keys, `FakeClock`, a real `KeyFailover`: a 429 on key 0 is answered by key 1 in the same call, no sleep; a per-minute limit on every key waits once and the call then succeeds; a "quota exceeded" 429 behaves the same; 402, `insufficient_quota` and 401 exhaust a key at once and the call succeeds on the next;
- every key exhausted before text raises `ModelKeysExhausted` with the per-key text and no key value; a 429 after text raises `TurnFailed` with the explanation and does not retry on another key;
- the `BAD_GENERATION` retry limit and message are unchanged; transient errors retry twice on the same key;
- two parallel calls on one failover share state; state is per model;
- `PUT /providers/{name}/settings` round-trips, rejects 0 and 1441 (422), 404 for an unknown provider; the setting is gone after the provider is deleted; adding or removing a key clears the failover state;
- the existing suite passes (updated rotation tests included); `npm run gen:api` no diff after commit; checks as task 1; CI green.

---

### Task 3: The stall in the turn graph

**Files:** new `agents/runtime/stall.py`, `agents/runtime/graph.py`, `agents/runtime/graph_run.py`, `agents/runtime/waiting.py` (the overview lists `agents/approvals/waiting.py`; see Interface gaps), tests: new `tests/agents/runtime/test_stall.py` and `test_stall_restart.py`.

**Interfaces:** Consumes task 2 and I13: `Approvals.create_stall(project_id, turn_id, step, preview) -> ApprovalView`, `ApprovalPart.kind` and `step`, the stall preview, events `turn.stalled` and `turn.resumed`, and contracts' handling of a stall's decision (Interface gaps, 1). Produces `StallState` in `TurnState` and the behavior: the model node stalls, the graph routes `model -> wait -> model`.

**Implementation notes:**
- `stall.py` holds `StallState` (frozen dataclass: `approval_id`, `provider_id`, `model`, `step`), the preview builder (`Preview(kind="text", title="Stalled: no usable key", body=describe_keys(...))`) and the two functions the graph calls. `TurnState` gains `stall: NotRequired[StallState | None]`; `StallState` joins the list in `turn_checkpointer` (its docstring says every added state type is listed there). The graph gets the `Approvals` it calls `create_stall` on as a new field of `TurnHooks`, wired next to the gate (if that wiring is circular, raise it in the plan issue; do not work around it).
- Model node: catch `ModelKeysExhausted` around `caller.call` (only without streamed text; with text the caller raises a plain `TurnFailed` and the turn fails as today, with `error_kind` set). On it: `create_stall(project_id, turn_id, step_count, preview)` (idempotent, so a re-run of the node after a restart finds the pending one), record `turn.stalled {turn_id, provider, model}` once per created approval, and return `{"stall": StallState(...)}` with no message and without spending the step budget.
- `route_model`: a `stall` routes to `wait` before the tool-call and turn-end checks. The `wait` node is the existing `step.wait` extended: with `state["stall"]` set it reads the approval from the store; pending calls `interrupt(None)` with no side effects before it (LangGraph re-runs the node on resume); approved calls `failover.clear(provider_id)`, records `turn.resumed {turn_id}` and returns `{"stall": None}`. The edge `wait -> tools` becomes conditional: `model` after a stall, `tools` as today. A crash between the clear and the checkpoint may repeat `turn.resumed`; events are informational, accepted and stated in the PR.
- The card: `graph_run.py` reads `stall` from the interrupted state next to `decisions`, and `handle_turn_waiting` adds an `ApprovalPart(kind="stall", step=...)` once (by approval id, as for tool approvals) so the session and the Inbox show it. No audit record is written for a stall.
- Try again and Stop are decided through the existing approvals route; the stall-specific handling in `ApprovalsService.decide` (approve resumes with no audit record; reject stops the turn through `sessions.stop`, whose `stop_waiting_turn` cancels the approval, ends the turn `stopped` and deletes the checkpoint) belongs to contracts task 2. This plan's tests prove it end to end.
- A stalled turn across a restart is an interrupted graph like any waiting turn: stage 2's recovery restores the card, the resume re-runs `wait`, reads the approval and goes on.
- Embedders (spec §3) and compaction never stall: they do not catch `ModelKeysExhausted`; embedders call `failover.call(..., wait=False)` and map `RateLimited` and `AllKeysExhausted` to `EmbedderUnavailable`.

**Tests (stop condition):** fake server and fake clock, real graph with a checkpointer:
- every key exhausted: the turn goes `waiting`; one `stall` approval with the per-key preview; `turn.stalled` recorded once; no model message; no audit record; step count unchanged;
- Try again: marks cleared, `turn.resumed` recorded, the model called again from the checkpoint, the turn `completed`, earlier messages intact;
- Stop (reject, and the agent's Stop route): approval `cancelled`, turn `stopped`, checkpoint deleted;
- Stop during a rate-limit wait (a `sleep` blocked on the fake clock) cancels the call cleanly and ends the turn `stopped`;
- stall, Try again, exhausted again: a second approval, not a reuse of the decided one; a re-run of the model node with a pending stall finds it (no duplicate);
- restart while stalled (new session manager over the same databases and checkpoint): the card is back, Try again resumes;
- text already streamed when the keys run out: the turn ends `failed` with the per-key text and `error_kind = "keys_exhausted"`, no stall;
- a tool approval and a stall in the same turn do not interfere;
- full suite, checks, `npm run gen:api` no diff; CI green.

---

### Task 4: The stalled-turn card, Try again on a failed turn, and the cooldown field

**Files:** new `frontend/src/pages/agents/StallCard.tsx` and `StallCard.test.tsx`, `frontend/src/pages/agents/ApprovalCard.tsx` (one line in contracts' dispatcher), the component that shows a failed turn in `frontend/src/pages/agents/` (Try again button), `frontend/src/pages/providers/ProviderCard.tsx` (the cooldown field) and its test, `frontend/src/pages/inbox/InboxPage.test.tsx` (a stall case).

**Interfaces:** Consumes task 3, the generated `schema.d.ts` types (`ApprovalPart.kind`, the turn's `error_kind`, `Provider.exhausted_cooldown_minutes`, the settings route), the dispatcher and `ApprovalControls` of contracts task 2. Produces the card, the Try again button and the field.

**Implementation notes:**
- `StallCard`: title "Stalled: no usable key", the provider and model, one row per key with its state and reason (the preview body rendered readably, never raw), Try again (approve) and Stop (reject) through contracts' `ApprovalControls` and the existing decision call. Settled states read "Resumed" and "Stopped". It renders in the session and in the Inbox through the dispatcher.
- A failed turn whose `error_kind` is `keys_exhausted` offers Try again, which re-sends the turn's trigger message through the existing send call.
- `ProviderCard`: a numeric "Exhausted key cooldown (minutes)" input, 1 to 1440, saved with the settings route, with one sentence on what it does (how long an exhausted key is skipped before one retry).
- shadcn components and error handling as the neighbouring cards; types only from `schema.d.ts`.

**Tests (stop condition):** component tests: the card shows each key's state and reason; Try again and Stop call the right endpoints and disable while pending; the settled states render; the dispatcher picks `StallCard` for `kind: "stall"` and the tool card for `tool`; the Inbox lists a stall with both buttons; a failed turn with `keys_exhausted` shows Try again and re-sends the trigger, other failures do not; the cooldown field saves, validates the range and shows an API error. `npm run lint`, `npm run format:check`, `npm test`, `npm run build`, `npm run e2e` (existing flow stays green; `flow.spec.ts` is onboarding's, so no case is added); CI green.

---

## Interface gaps

Still open after the overview's revision:
1. **A stall's decision in `ApprovalsService.decide`** (contracts owns `agents/approvals/service.py`): approve resumes with no audit record and no `approval.decided` audit entry; reject calls `sessions.stop` for the agent. I13 names neither; contracts task 2 needs them, or this plan's task 3 cannot prove Try again and Stop.
2. **The cooldown setting edits files the overview gives to embedders** (`providers/models.py`, `providers/service.py`; the route in `api/routers/providers.py` has no owner listed). This plan adds only its own lines there: the `Provider.exhausted_cooldown_minutes` field (task 1), the app-`settings` read and write, `failover.clear` calls and `PUT /providers/{name}/settings` (task 2). The spec calls it "a provider setting"; no schema column exists, so it is stored in the existing `settings` key-value table. Confirm, or add a `providers` column to the contracts migration.
3. **Additions to I21** this plan makes in `failover.py`: `Clock`, `KeyFailover(clock)`, `states`, `clear`, `describe_keys`. Confirm they are in scope for the producer.
4. **`error_kind` on a turn failure:** I13 adds the field to `turn.ended` and the turn view; this plan sets it by giving `TurnFailed` an `error_kind` that the executor's failure recording must carry through. `agents/runtime/executor.py` and `waiting.py`'s end report are not in the shared-files list, and contracts' part of the wiring is unstated. Say who edits the executor's failure path.
5. **Path mismatch:** "Files shared" lists `agents/approvals/waiting.py`; the file is `agents/runtime/waiting.py`. This plan edits the latter.
6. **Stall wiring into the graph:** the model node needs the `Approvals` (for `create_stall`); the plan adds a `TurnHooks` field. `agents/runtime/hooks.py`, `sessions.py` and `api/services.py` wiring lines are this plan's under that reading; confirm.
