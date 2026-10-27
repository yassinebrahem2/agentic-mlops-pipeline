> **Draft.** Missing: the lead's advisor review.

# Stage 3 — Rate Limits Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works. Shared context: `docs/superpowers/plans/2026-10-28-stage-3-overview.md`.

**Goal:** turns survive rate limits (spec §2):
- a 429 moves to the next key at once; a per-minute limit never ends a turn;
- key state is kept per provider, key and model, shared by the whole app;
- when every key is exhausted the turn pauses on a `stall` approval (card with Try again and Stop) instead of failing, and no work is lost.

**Branch:** `plan/stage-3-rate-limits`. **Consumes:** task 1 none; tasks 2 to 4 contracts task 2 (I13: the `stall` approval kind, `kind` on `ApprovalPart`, `turn.stalled` and `turn.resumed`). **Produces:** `KeyFailover`, `AllKeysExhausted` (I21) from task 1 (early PR); the stall; the exhausted-key cooldown setting.

**Read first:** spec §2, §3 (the embedder never stalls) and §12 (stalled-turn card, cooldown field); overview I13 and I21; stage 2 approvals plan (`2026-10-24-stage-2-approvals.md`) for how a turn waits and resumes. Existing code: `providers/errors.py`, `providers/bridge.py` (`ResolvedModel`), `agents/runtime/model_caller.py`, `agents/runtime/graph.py`, `agents/runtime/graph_run.py`, `agents/runtime/waiting.py`, `agents/approvals/{store,service}.py`. The reference engine (`D:\SCRIPTS\Multimodal RAG Agent\src\mmrag\providers\retry_engine.py`) is for the concept only: it waits on one key and swaps after a streak; the rule here is different (the rule below is the user's). Never copy it.

**Fake clock:** every time rule is tested with `FakeClock` (`backend/tests/providers/fake_clock.py`, created by task 1): `monotonic()`, `wall()` and `async sleep(s)` that advance both clocks and record `s`, so no test waits in real time.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | `KeyFailover` and the error classification | none |
| 2 | The cooldown setting and the model caller on `KeyFailover` | task 1 |
| 3 | The stall in the turn graph | task 2, contracts task 2 |
| 4 | The stalled-turn card and the cooldown field | task 3 |

Task 1 gets an early PR from `plan/stage-3-rate-limits-task1`: embedders consumes `KeyFailover`.

---

### Task 1: `KeyFailover` and the error classification

**Files:** new `backend/src/mlops_agents/providers/failover.py`, `providers/errors.py` (classification), `providers/models.py` (`Provider.exhausted_cooldown_minutes`, `DEFAULT_EXHAUSTED_COOLDOWN_MINUTES = 15`, the one source), new `backend/tests/providers/fake_clock.py`, new `tests/providers/test_failover.py` (and `test_failover_concurrency.py` if past 400 lines), `tests/providers/test_errors.py`.

**Interfaces:** Consumes the existing `Provider`, `KeyHint`, `ProviderCallError`, `ProviderErrorKind`. Produces (I21, with the names the overview leaves open filled in):

```python
KeyStateName = Literal["exhausted_daily", "billing", "rejected"]
class KeyState(BaseModel): index: int; state: KeyStateName; since: datetime

class Clock(Protocol):
    def monotonic(self) -> float: ...
    def wall(self) -> datetime: ...
    async def sleep(self, seconds: float) -> None: ...

class AllKeysExhausted(Exception): keys: list[KeyState]      # every key of the provider, for that model
class AttemptAborted(Exception): error: ProviderCallError    # an attempt raises this instead of the error when the call must not move to another key (text already streamed)

class KeyFailover:       # one per app
    def __init__(self, clock: Clock | None = None) -> None   # None: the system clock
    async def call[T](self, provider: Provider, model: str, attempt: Callable[[int], Awaitable[T]]) -> T
    def exhausted(self, provider_id: str, model: str) -> list[KeyState]   # for the explanation after AttemptAborted
    def clear(self, provider_id: str) -> None                              # every key and model of the provider: Try again, and a key added or removed
def describe_keys(provider: Provider, keys: list[KeyState]) -> str        # the one per-key explanation, used by the failure text and the stall card
```

`attempt(key_index)` makes ONE call with that key and raises `ProviderCallError` (already classified by the caller, secrets scrubbed). Anything else it raises (including `asyncio.CancelledError`) passes through untouched.

**Implementation notes:**
- **State** lives in one dict keyed `(provider_id, key_index, model)`; each entry: `last_limited` and `retry_after` (monotonic seconds), `exhausted` (`KeyStateName | None`), `since` (wall time of the mark), `probe_at` (monotonic). Plus a cursor per `(provider_id, model)`: the index to try first. A provider with no keys (self-hosted) has one keyless slot, index 0.
- **Concurrency:** every read-then-write of the state is one synchronous block with no `await` inside, so no lock is needed and none is ever held across `attempt` or `sleep`. Several calls (the four kickoff audits) share the state; two calls limited on the same key within a minute never exhaust it between them.
- **Loop of `call`:**
  1. Pick (synchronously). Probes first: an exhausted key whose `probe_at <= now` (taking it sets `probe_at = now + cooldown`, so one call probes it at a time). Then ready keys: not exhausted and either never limited or past its clear time (`last_limited + min(retry_after or 60, 60)`), in index order starting at the cursor.
  2. If nothing is pickable and no key is non-exhausted: raise `AllKeysExhausted(all keys' states)`. If some key is limited: `await clock.sleep(earliest clear time or probe time - now)`, then pick again. The wait is capped at 60 s per round, so a `Retry-After` of an hour never holds a call; the rule below then decides the key.
  3. `attempt(index)`. Success: clear that key's `last_limited` and `exhausted`, set the cursor to it, return.
  4. `ProviderCallError` by kind:
     - `RATE_LIMIT`: a probe attempt stays exhausted (`probe_at` already pushed); otherwise, if the key's `last_limited` is at least 60 s old (`>=`, tested at 59.9 and 60.0) it becomes `exhausted_daily`; else record `last_limited = now` and `retry_after`. Cursor moves to the next index. Loop at once, with no sleep and no counter: rate limits never spend a retry budget.
     - `QUOTA` (billing): exhausted `billing` at once. `AUTH`: exhausted `rejected` at once. Loop to the next key.
     - `TRANSIENT`, `UNREACHABLE`: retry the same key after `TRANSIENT_BACKOFFS = (1.0, 2.0)` seconds (moved here from the model caller; one counter per call), then raise the error. A different key would not fix it.
     - any other kind (`BAD_GENERATION`, `BAD_REQUEST`, `UNKNOWN`): raise at once.
  5. `AttemptAborted(error)`: apply step 4's state change for `error` (so the key's limit is remembered), then re-raise `AttemptAborted` without trying another key.
- A key added or removed shifts indices, so ProvidersService (task 2) calls `clear(provider_id)` on both; there is no other way for indices to go stale.
- Known consequence of the user's rule, to keep as specified: a key limited once, idle for over a minute and limited again before any success is marked exhausted; it comes back after the cooldown probe.
- `describe_keys` lines read like "Key ...abcd: daily limit reached (since 14:03)", "billing: out of credit", "rejected: the provider refused this key"; it takes the key hints from `provider.keys`. No key value ever appears.
- Classification in `errors.py` (`_kind`): every HTTP 429 is `RATE_LIMIT`, whatever its wording ("quota exceeded" included). `QUOTA` only for HTTP 402 or the code `insufficient_quota` (attribute, body or nested `error.code`; a 429 carrying it is `QUOTA`). The openai SDK raises a plain `APIStatusError` for 402, so test `status_code == 402` before the generic branch. `AUTH` unchanged (401, 403). Remove the `"quota" in str(e)` test. `Retry-After` stays numeric seconds only (an HTTP date gives `None`).

**Tests (stop condition):** all with `FakeClock`, an `attempt` script and no network:
- a 429 on key 0 moves to key 1 with no sleep recorded; success clears the limit time;
- a key limited again after 59.9 s is not exhausted, after 60.0 s it is `exhausted_daily` (and `AllKeysExhausted` for a single key);
- every key limited under a minute ago: exactly one sleep, until the earliest clear time (`Retry-After` 5 s gives 5, none gives 60, 3600 gives 60), then that key is tried;
- a success between two limits resets the clock (never exhausted);
- state is per model: exhausted for `m1`, fine for `m2`; per provider too;
- exhausted keys are skipped; after the cooldown exactly one probe, concurrent calls do not probe together, a failed probe stays exhausted for another cooldown, a success clears it; `clear(provider_id)` clears all;
- billing and rejected exhaust at once and show their state; every key exhausted raises `AllKeysExhausted` with each key's state and `since`;
- transient and unreachable retry with the two backoffs then raise; bad request, bad generation and unknown raise at once; rate limits never spend the transient budget (ten 429s then success works);
- `AttemptAborted` records the limit, does not move to another key, re-raises; `exhausted()` then lists it;
- two concurrent calls on shared state (one key limited by the first is seen by the second); a cancel during a sleep propagates and leaves the state valid; `describe_keys` output holds no key value;
- errors: 429 with "quota exceeded" is `RATE_LIMIT`; 429 `insufficient_quota` and HTTP 402 are `QUOTA`; 401 and 403 `AUTH`; 5xx `TRANSIENT`; the retry-after parsing cases;
- `uv run ruff check`, `uv run ruff format`, `uv run pyright`, `uv run pytest`; CI green (both OS jobs).

**PR:** early PR into `main`, `Closes #<task issue>`, from `plan/stage-3-rate-limits-task1`; the task issue says so.

---

### Task 2: The cooldown setting and the model caller on `KeyFailover`

**Files:** `agents/runtime/model_caller.py`, `providers/service.py`, `api/routers/providers.py`, `api/services.py` (its own lines), `agents/runtime/sessions.py` (the `ModelCaller(...)` construction line), `providers/models.py`, `tests/fakes/fake_openai.py` (new functions only), `tests/agents/runtime/test_model_caller.py` (updated) and new `tests/agents/runtime/test_model_caller_failover.py`, `tests/providers/test_service.py`, `tests/api/` provider route tests, `frontend/openapi.json` and `frontend/src/api/schema.d.ts` (regenerated).

**Interfaces:** Consumes task 1, `ResolvedModel`, `ProvidersService`. Produces `ModelCaller(providers, http, failover)`, `ModelKeysExhausted(TurnFailed)` (carries `provider`, `model`, `keys`), `PUT /api/providers/{name}/settings` with `{exhausted_cooldown_minutes: int}` (1 to 1440), and `Provider.exhausted_cooldown_minutes` filled in.

**Implementation notes:**
- `api/services.py` creates the one `KeyFailover` and hands it to `ProvidersService` (for `clear` on key change) and to `ModelCaller` (through `sessions.py`).
- The cooldown is stored in the existing app `settings` key-value table under `provider.<id>.exhausted_cooldown_minutes` (no migration, so no dependency on contracts); `get_provider` and `list_providers` fill it, default `DEFAULT_EXHAUSTED_COOLDOWN_MINUTES`; `delete_provider` removes it. `add_key`, `remove_key` and `delete_provider` call `failover.clear(provider.id)`.
- `ModelCaller.call` keeps: building the client, streaming, `on_text`, usage, tool-call indexes, the `BAD_GENERATION` corrective retry (`MAX_REJECTED_RETRIES`, an outer loop around `failover.call` that appends the corrective message). It loses: `MAX_ATTEMPTS`, `TRANSIENT_BACKOFFS`, key rotation and `rate_limited_keys`, and the `sleep` constructor argument (the clock is the failover's; existing tests move to `FakeClock`).
- `attempt(key_index)` builds the model with that key, calls `classify(exc, secrets=model.keys)` and raises the `ProviderCallError`. Once any text has streamed in the call, it raises `AttemptAborted(error)` instead, so the call cannot move to another key after output. `ProviderCallError` of any kind, after text, becomes `TurnFailed` as today.
- Endings, with one text from `describe_keys`:
  - `AllKeysExhausted` before any text: raise `ModelKeysExhausted` (a `TurnFailed`, so every current caller, compaction included, fails as before until task 3 catches it in the model node). Text: `"<provider>: no usable key for <model>. " + describe_keys(...)`.
  - `AttemptAborted` (text streamed): `TurnFailed` with the error's message plus `describe_keys(provider, failover.exhausted(...))`.
  - Billing or rejection with nothing left (every key `billing` or `rejected`, none daily): also `providers.mark_needs_attention(provider_id, message)` as today.
  - Other errors: `TurnFailed(error.message)` as today.
- Fake OpenAI server: new functions of their own (rate-limits owns them; embedders adds `/embeddings` separately): `FakeState.script_for_key(key, [outcomes])`, consumed per request by the `Authorization` bearer key. Outcomes: `"ok"`, `("429", retry_after | None)`, `"429-quota-wording"` (message "You exceeded your current quota", code `rate_limit_exceeded`), `"402"`, `"insufficient_quota"` (429), `"401"`. An empty script means the existing behavior.
- `npm run gen:api` once the route exists; commit the result.

**Tests (stop condition):**
- with the fake server, two keys, a fake clock and `ModelCaller` over a real `KeyFailover`: a 429 on key 0 is answered by key 1 in the same call with no sleep; a per-minute limit on every key waits once, and the call then succeeds, never ending the turn; a 429 worded "quota exceeded" behaves the same; 402 and `insufficient_quota` exhaust a key at once and the call succeeds on the next; 401 likewise;
- every key exhausted before text raises `ModelKeysExhausted` with the per-key text and no key value in it; a 429 after text streamed raises `TurnFailed` with the explanation and does not retry on another key;
- the `BAD_GENERATION` retry limit and corrective message are unchanged; transient errors retry twice on the same key;
- two `ModelCaller` calls in parallel on one failover share state; state is per model (the same key works for a second model);
- `PUT /providers/{name}/settings` round-trips, rejects 0 and 1441 (422), 404 for an unknown provider; the setting is gone after the provider is deleted; adding or removing a key clears the failover state;
- the whole existing suite passes (updated rotation tests included); `npm run gen:api` shows no diff after commit; checks as task 1; CI green.

---

### Task 3: The stall in the turn graph

**Files:** new `agents/runtime/stall.py`, `agents/runtime/graph.py`, `agents/runtime/graph_run.py`, `agents/runtime/waiting.py`, `agents/approvals/service.py`, tests: new `tests/agents/runtime/test_stall.py`, `test_stall_restart.py`, `tests/agents/approvals/` stall decide tests.

**Interfaces:** Consumes task 2, I13 (`ApprovalKind`, `ApprovalPart.kind`, the `stall` approval and its preview, `turn.stalled`, `turn.resumed`), the approvals store's stall create keyed by turn and step (see Interface gaps). Produces `StallState` in `TurnState` and the behavior: model node stalls, `model -> wait -> model`.

**Implementation notes:**
- `stall.py` holds `StallState` (a frozen dataclass: `approval_id`, `provider_id`, `model`, `step`), the stall preview builder (`Preview(kind="text", title="Stalled: no usable key", body=describe_keys(...))`), and the two functions the graph calls: `record_stall(...)` and `await_stall(...)`. `TurnState` gains `stall: NotRequired[StallState | None]`; `StallState` joins the list in `turn_checkpointer` (its docstring says every added state type must be listed there).
- Model node: catch `ModelKeysExhausted` around `caller.call` (only when no text streamed in the step; with text the caller raises a plain `TurnFailed` and the turn fails as today). On it: get-or-create the pending stall approval for `(turn_id, step_count)` (a re-run of the node after a restart finds the pending one), record `turn.stalled {turn_id, provider, model}` once per created approval, and return `{"stall": StallState(...)}` with no message and without spending the step budget. A decided stall at the same step (Try again, then exhausted again) gets a new approval.
- `route_model`: a pending `stall` routes to `wait` (before the tool-call and turn-end checks). The `wait` node is the existing `step.wait` extended: when `state["stall"]` is set it reads the approval from the store; pending calls `interrupt(None)` (like the gate's wait, no side effects before it, because LangGraph re-runs the node on resume); approved calls `failover.clear(provider_id)`, records `turn.resumed {turn_id}` and returns `{"stall": None}`. The edge `wait -> tools` becomes conditional: `model` after a stall, `tools` as today. A crash between the clear and the checkpoint may repeat `turn.resumed`; events are informational, so this is accepted and stated in the PR.
- The agent message gets the card: `graph_run.py` reads `stall` from the interrupted state next to `decisions`, and `handle_turn_waiting` adds an `ApprovalPart(kind="stall")` (once, by approval id, as it does for tool approvals) so the session and the Inbox show it; no audit record is written for a stall.
- Try again and Stop: `ApprovalsService.decide` on a stall approval:
  - approve: as for any approval (`resume_turn`), no audit record; the node above clears the marks, so no work is lost;
  - reject (the card's Stop and the Inbox): calls `sessions.stop` for the agent, whose `stop_waiting_turn` cancels the approval, ends the turn `stopped` and deletes the checkpoint. Stop through the agent's own Stop route behaves the same.
- A turn stalled across a restart: it is an interrupted graph like any waiting turn, so stage 2's recovery restores the card; the resume re-runs `wait`, reads the approval and goes on.
- Embedders (spec §3) and compaction never stall: they do not catch `ModelKeysExhausted` and fail or degrade as their owners define.

**Tests (stop condition):** with the fake server and fake clock, on the real graph with a checkpointer:
- every key exhausted: the turn goes to `waiting`, one `stall` approval exists with the per-key preview, `turn.stalled` recorded once, no model message, no audit record, step count unchanged;
- Try again (approve): marks cleared, `turn.resumed` recorded, the model is called again from the checkpoint, the turn finishes `completed`, nothing lost (messages before the stall intact);
- Stop (reject, and the Stop route): approval `cancelled`, turn `stopped`, checkpoint deleted;
- Stop during a rate-limit wait (a `sleep` blocked on the fake clock) cancels the call cleanly and ends the turn `stopped`;
- stall, Try again, exhausted again: a second approval, not a reuse of the decided one; a re-run of the model node with a pending stall finds it (no duplicate);
- restart while stalled (new session manager over the same databases and checkpoint): the card is back, Try again resumes;
- text already streamed when keys run out: the turn ends `failed` with the per-key text, no stall;
- a tool-approval turn and a stall in the same turn do not interfere;
- full suite, checks, `npm run gen:api` no diff; CI green.

---

### Task 4: The stalled-turn card and the cooldown field

**Files:** new `frontend/src/pages/agents/StallCard.tsx` and `StallCard.test.tsx`, `frontend/src/pages/agents/ApprovalCard.tsx` (one line in the dispatcher contracts adds), `frontend/src/pages/providers/ProviderCard.tsx` (the cooldown field) and its test, `frontend/e2e/` only if the flow needs it (see below), `frontend/src/pages/inbox/InboxPage.test.tsx` (a stall case).

**Interfaces:** Consumes task 3 and the generated `schema.d.ts` types (`ApprovalPart.kind`, `Provider.exhausted_cooldown_minutes`, the settings route); the dispatcher of contracts task 2. Produces the card and the field.

**Implementation notes:**
- `StallCard`: title "Stalled: no usable key", the provider and model, one row per key with its state and reason (the preview body, rendered readably, never raw), buttons Try again (the existing decision call, approve) and Stop (the existing Stop for the agent). Settled states read "Resumed" and "Stopped". It renders in the session and in the Inbox through the dispatcher.
- `ProviderCard`: a numeric "Exhausted key cooldown (minutes)" input, 1 to 1440, saved with the settings route, with one sentence on what it does (how long an exhausted key is skipped before one retry).
- Use the shadcn components and error handling the neighbouring cards use; types come only from `schema.d.ts`.

**Tests (stop condition):** component tests: the card shows each key's state and reason; Try again and Stop call the right endpoints and disable while pending; the settled states render; the dispatcher picks `StallCard` for `kind: "stall"` and the tool card for `tool`; the Inbox lists a stall approval with both buttons; the cooldown field saves, validates the range and shows an API error. `npm run lint`, `npm run format:check`, `npm test`, `npm run build`, and `npm run e2e` (the existing flow stays green; no new e2e case unless the owner of the onboarding e2e asks, since `flow.spec.ts` is onboarding's). CI green.

---

## Interface gaps

1. **`ProviderView` (I21)** does not exist: the code's model is `Provider` (`providers/models.py`). This plan uses `Provider` and gives it `exhausted_cooldown_minutes`. I21 should read `Provider`.
2. **I21 leaves open** `KeyState`'s fields, the clock, how an attempt refuses failover after streamed text, and `clear`/`exhausted`. This plan adds `Clock`, `AttemptAborted`, `exhausted()`, `clear()` and `describe_keys()` (task 1); embedders only needs `call` and `AllKeysExhausted`.
3. **The stall in the approvals store (contracts task 2).** The overview says "keyed by turn and step". This plan needs: an idempotent create of a `stall` approval that returns the pending one for `(turn_id, step)` if it exists, and uniqueness only among pending stalls (a decided stall at a step must not block a second stall at the same step after Try again). Contracts must name the method and make the unique index partial; the same task fixes `tool_call_id` null in `ApprovalPart` and `ApprovalView`.
4. **Shared files missing from the overview's list:** `agents/runtime/graph_run.py`, `agents/runtime/waiting.py`, `agents/approvals/service.py`, `agents/runtime/sessions.py` (one construction line) and `providers/service.py`, `providers/models.py`, `api/routers/providers.py` (embedders also edits the provider files for model types). Each plan should add only its own lines there; the lead to confirm rate-limits owns the first four for the stall.
5. **The cooldown storage:** the spec calls it "a provider setting"; there is no per-provider settings table. This plan stores it in the existing app `settings` key-value table (no migration). If a column on `providers` is preferred, it needs contracts' migration (a Decision).
6. **Try again after a failure with streamed text (spec §2, last bullet):** it "sends the trigger message again", but a failed turn today stores only its error text, with no code the UI can recognise. Needs a decision on how the UI knows a failure is "keys exhausted" (a field on the stored failure notice, for example); not in this plan until decided.
7. **The 60 s cap on a wait round** (a `Retry-After` of an hour waits 60 s, then the "limited again after a minute" rule decides the key) interprets spec §2's "waits only until the earliest clears". Confirm.
