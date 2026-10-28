# Stage 3 — Rate Limits Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works. Shared context: `docs/superpowers/plans/2026-10-28-stage-3-overview.md`.

**Goal:** turns survive rate limits (spec §2):
- a 429 moves to the next key at once; a per-minute limit never ends a turn;
- key state is kept per provider, key and model, shared by the whole app;
- when every key is exhausted the turn pauses on a `stall` approval (card with Try again and Stop) instead of failing, and no work is lost.

**Branch:** `plan/stage-3-rate-limits`. **Consumes:** task 1 none; tasks 2 to 4 I13 (contracts task 3: `Approvals.create_stall`, the `stall` kind, `kind` and `step` on `ApprovalPart`, the decision of a stall, `turn.stalled`, `turn.resumed`, `error_kind` on the turn and on the failed agent message's `MessageView`, the card dispatcher and `ApprovalControls`) and contracts task 1 (`Provider.cooldown_minutes` with `providers.cooldown_minutes`, `turns.error_kind`, and the types `ErrorKind`, `TurnEndedData.error_kind` and `MessageView.error_kind`, which this plan only writes and reads). **Produces:** `KeyFailover`, `AllKeysExhausted`, `RateLimited`, `StreamedTextFailure`, `Clock` (I21) and `Services.failover` (the one instance) from task 1 (early PR); the stall; the cooldown route and field.

**Read first:** spec §2, §3 (the embedder never stalls) and §12 (stalled-turn card, cooldown field); overview "Shared by contracts", I13, I14 (`EmbedderUnavailable`), I21 and "Files shared"; the stage 2 approvals plan (`2026-10-24-stage-2-approvals.md`) for how a turn waits and resumes. Existing code: `providers/errors.py`, `providers/bridge.py` (`ResolvedModel`), `agents/runtime/model_caller.py`, `graph.py`, `graph_run.py`, `waiting.py`, `executor.py`, `turn_end.py`, `hub.py`, `store/turns.py`, `agents/approvals/{store,service}.py`. The reference engine (`D:\SCRIPTS\Multimodal RAG Agent\src\mmrag\providers\retry_engine.py`) is for the concept only: it waits on one key and swaps after a streak, which is not the user's rule below. Never copy it.

**Fake clock:** every time rule is tested with `FakeClock` (`backend/tests/providers/fake_clock.py`, created by task 1), an implementation of `Clock`: `now()` and `async sleep(s)`; `sleep` advances the clock and records `s`, so no test waits in real time.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | `KeyFailover` and the error classification | none |
| 2 | The model caller on `KeyFailover`, `error_kind` and the cooldown route | task 1, contracts tasks 1 and 3 |
| 3 | The stall in the turn graph | task 2, contracts task 3 |
| 4 | The stalled-turn card, Try again on a failed turn, and the cooldown field | task 3 |

Task 1 gets an early PR from `plan/stage-3-rate-limits-task1`: embedders consumes `KeyFailover`.

---

### Task 1: `KeyFailover` and the error classification

**Files:** new `backend/src/mlops_agents/providers/failover.py`, `providers/errors.py` (classification), `api/services.py` (the one `KeyFailover`, below), new `backend/tests/providers/fake_clock.py`, new `tests/providers/test_failover.py` (a second file past 400 lines), `tests/providers/test_errors.py`.

**Interfaces:** Consumes `Provider` (its `keys` and `id`; the cooldown arrives as the `cooldown_minutes` argument of `call`, so task 1 depends on nothing of contracts'), `ProviderCallError`, `ProviderErrorKind`, `classify`. Produces I21 exactly as the overview has it:

```python
KeyStatus = Literal["ok", "limited", "exhausted_daily", "billing", "rejected"]
class KeyState(BaseModel): key_index: int; status: KeyStatus; since: datetime | None
class AllKeysExhausted(Exception): keys: list[KeyState]
class RateLimited(Exception): retry_after_s: float
class StreamedTextFailure(Exception): cause: BaseException
class Clock(Protocol):  def now(self) -> datetime; async def sleep(self, seconds: float) -> None
class KeyFailover:
    def __init__(self, clock: Clock | None = None) -> None   # None: the system clock
    async def call[T](self, provider: Provider, model: str, attempt: Callable[[int | None], Awaitable[T]], *, cooldown_minutes: int, wait: bool = True) -> T: ...   # cooldown_minutes: required; callers pass Provider.cooldown_minutes
    def states(self, provider: Provider, model: str) -> list[KeyState]
    def clear(self, provider: Provider) -> None                 # every key and model of the provider (Try again)
    def describe_keys(self, states: list[KeyState]) -> str      # the one wording: stall card, failure text, embedder message
```

Decided here (the producer's call): `Clock.now()` returns an aware UTC `datetime`, because `KeyState.since` is one; the system clock is `datetime.now(UTC)` and `asyncio.sleep`.

**Implementation notes:**
- `attempt(key_index)` makes ONE call and raises the provider's raw exception; `KeyFailover` classifies it with `classify(exc)` (kind and `retry_after` only). What it does not handle it re-raises as the raw exception, unchanged, so the caller classifies it again with the key values to scrub. `asyncio.CancelledError` and non-provider exceptions pass through. A provider with no keys (`provider.keys` empty) gets `attempt(None)` once, with no failover and no state.
- **State** is one dict keyed `(provider.id, key_index, model)`; each entry: `last_limited` and `clear_at` (datetimes), `exhausted` (`exhausted_daily | billing | rejected | None`), `since`, `probe_at`. Plus a cursor per `(provider.id, model)`: the index to try first. `states` derives `KeyState.status`: `exhausted` if set, else `limited` while `now < clear_at`, else `ok`.
- **Concurrency:** every read-then-write of the state is one synchronous block with no `await` inside, so no lock is needed and none is held across `attempt` or `sleep`. Calls (the four kickoff audits) share the state; two calls limited on one key within a minute never exhaust it between them.
- **Loop of `call`:**
  1. Pick (synchronously). Probes first: an exhausted key whose `probe_at <= now` (taking it sets `probe_at = now + cooldown`, so one call probes at a time; `cooldown = timedelta(minutes=cooldown_minutes)`). Then ready keys: not exhausted and `now >= clear_at`, in index order from the cursor.
  2. Nothing pickable and no non-exhausted key: raise `AllKeysExhausted(states)`. Some key limited: with `wait=False` raise `RateLimited(retry_after_s=earliest clear_at - now)`; with `wait=True`, `await clock.sleep(that)` (never more than 60 s; the earliest probe time counts too) and pick again.
  3. `attempt(index)`. Success: clear the key's `last_limited`, `clear_at` and `exhausted`, set the cursor to it, return.
  4. Failure, by `classify(exc).kind`:
     - `RATE_LIMIT`: a probe attempt stays exhausted (`probe_at` already pushed); otherwise, when `now - last_limited >= 60 s` (tested at 59.9 and 60.0) the key becomes `exhausted_daily`; else `last_limited = now` and `clear_at = now + min(retry_after or 60, 60)` seconds. The cursor moves to the next index and the loop continues at once with no sleep and no counter: rate limits never spend a retry budget.
     - `QUOTA` (billing): exhausted `billing` at once. `AUTH`: exhausted `rejected` at once. Next key.
     - `TRANSIENT`, `UNREACHABLE`: retry the same key after `TRANSIENT_BACKOFFS = (1.0, 2.0)` seconds (moved here from the model caller; one counter per call), then re-raise the raw exception. A different key would not fix it.
     - any other kind: re-raise at once.
  5. `StreamedTextFailure(cause)`: apply step 4's state change for `cause`, then re-raise the `StreamedTextFailure` without trying another key.
- **The one instance:** `build_services` (`api/services.py`) builds a single `KeyFailover` (system clock) and keeps it on `Services` as `failover`, so embedders task 1 can use it; nothing else constructs one outside tests. This is why the early PR touches `api/services.py`.
- Callers that add or remove a key call `clear(provider)` (task 2), since indices shift.
- Known consequence of the user's rule, kept as specified: a key limited once, idle over a minute and limited again before any success is marked exhausted; it returns through the cooldown probe.
- `describe_keys` lines read like "Key 1: daily limit reached (since 14:03 UTC).", "Key 2: billing, out of credit.", "Key 3: rejected by the provider.", "Key 4: rate limited (since 14:05 UTC).", "Key 5: ok."; one line per key, joined with a single space into one string, so a card shows it as plain text. Keys are named by position, never by value. Times are absolute, `HH:MM UTC`, because the text is stored and read later (a relative "clears in" would be stale). `KeyState.since` is the time the key became exhausted for an exhausted key, and its last rate limit for a `limited` one.
- Classification in `errors.py` (`_kind`): every HTTP 429 is `RATE_LIMIT`, whatever its wording ("quota exceeded" included). `QUOTA` only for HTTP 402 or the code `insufficient_quota` (attribute, body or nested `error.code`; a 429 carrying it is `QUOTA`). The openai SDK raises a plain `APIStatusError` for 402, so test `status_code == 402` before the generic branch. `AUTH` unchanged. Remove the `"quota" in str(e)` test. `Retry-After` stays numeric seconds (an HTTP date gives `None`).

**Tests (stop condition):** all with `FakeClock`, an `attempt` script and no network:
- a 429 on key 0 moves to key 1 with no sleep recorded; a success clears the limit time;
- a key limited again after 59.9 s is not exhausted, after 60.0 s it is `exhausted_daily` (a single key then raises `AllKeysExhausted`);
- every key limited under a minute ago: exactly one sleep, until the earliest clear time (`Retry-After` 5 s gives 5, none gives 60, 3600 gives 60), then that key is tried; with `wait=False` the same state raises `RateLimited` with that delay and records no sleep;
- a success between two limits resets the clock; ten 429s then a success works (no budget);
- state is per model and per provider;
- exhausted keys are skipped; after `cooldown_minutes` exactly one probe, concurrent calls do not probe together, a failed probe stays exhausted another cooldown, a success clears it; `clear(provider)` clears all; a different `cooldown_minutes` argument changes the probe time;
- billing (`insufficient_quota` and 402) and rejected (401, 403) exhaust at once with their status; every key exhausted raises `AllKeysExhausted` with each key's status and `since`; `states` reports `ok` and `limited` too;
- transient and unreachable retry with the two backoffs then re-raise the raw exception; bad request, bad generation and unknown re-raise at once;
- `StreamedTextFailure` records the limit, never moves to another key, re-raises; a provider with no keys gets `attempt(None)` once;
- two concurrent calls on shared state; a cancel during a sleep propagates and leaves the state valid; `describe_keys` output holds no key value;
- `build_services` exposes exactly one `KeyFailover` (`services.failover`), the same object on every access;
- errors: 429 worded "quota exceeded" is `RATE_LIMIT`; 429 `insufficient_quota` and HTTP 402 are `QUOTA`; 401 and 403 `AUTH`; 5xx `TRANSIENT`; the `Retry-After` cases;
- `uv run ruff check`, `uv run ruff format`, `uv run pyright`, `uv run pytest`; CI green (both OS jobs).

**PR:** early PR into `main`, `Closes #<task issue>`, from `plan/stage-3-rate-limits-task1`.

---

### Task 2: The model caller on `KeyFailover`, `error_kind` and the cooldown route

**Files:** `agents/runtime/model_caller.py`, `agents/runtime/sessions.py` (the `ModelCaller(...)` line), `agents/settings.py` (the needs-attention refusal removed, Decision #384), `providers/service.py` (`clear_attention`), the `error_kind` path named below (`agents/runtime/store/messages.py`, `agents/runtime/executor.py`, `waiting.py`, `turn_end.py`, `hub.py`, `hooks.py`, `store/turns.py`, `events/observer.py`, `agents/messaging/inbound.py`, `agents/pr_sync.py` and the test observer fakes under `tests/agents/runtime/`), `providers/service.py` (its key-failover lines) and `api/routers/providers.py` (the route), `api/services.py` (its own lines), `tests/fakes/fake_openai.py` (new functions only), `tests/agents/runtime/test_model_caller.py` (updated), new `tests/agents/runtime/test_model_caller_failover.py` and `test_error_kind.py`, `tests/providers/test_service.py`, provider route tests, `frontend/openapi.json` (regenerated; `schema.d.ts` is gitignored).

**Interfaces:** Consumes task 1, `ResolvedModel`, `ProvidersService`, `Provider.cooldown_minutes` and `turns.error_kind` (contracts task 1). Produces `ModelCaller(providers, http, failover)`; `TurnFailed.error_kind: Literal["keys_exhausted"] | None`; `ModelKeysExhausted(TurnFailed)` (carries `provider_id`, `model`, `keys: list[KeyState]`, `description: str`), `ModelCaller.reset_keys(provider_id)`; `PUT /api/providers/{name}/settings` with `{cooldown_minutes: int}` (1 to 1440).

**Implementation notes:**
- `build_services` hands the one `KeyFailover` (built in task 1) to `ProvidersService` and `ModelCaller`. `ProvidersService.add_key`, `remove_key` and `delete_provider` call `failover.clear(provider)`; the settings route writes `providers.cooldown_minutes` (the column and `Provider` field are contracts').
- **How the caller gets a `Provider`:** `ModelCaller.call` receives a `ResolvedModel` (provider id, key values, no `Provider`). Per call it reads `provider = await providers.get_provider_by_id(model.provider_id)` and `keys = await providers.keys_for(provider.id)`, builds each attempt's client from `dataclasses.replace(model, keys=tuple(keys)).build(http, key_index)`, and passes `provider` and `cooldown_minutes=provider.cooldown_minutes` to `failover.call`, so indices, `provider.keys` and the key values come from one read. A key added or removed during the call calls `clear` and can leave an index out of range: the attempt then raises an internal `_StaleKeys`, and the caller re-reads and restarts the call once (a second `_StaleKeys` fails the turn with the normal unexpected-failure text). Secrets to scrub are `model.keys` plus the fresh `keys`.
- **How the graph reaches the failover:** it does not; `create_turn_graph` only has `caller`. `ModelCaller.reset_keys(provider_id: str)` reads the provider and calls `failover.clear(provider)`; `ModelKeysExhausted` carries `provider_id`, `model`, `keys` and `description` (the `describe_keys` text), so the model node builds the stall preview from it and the `wait` node calls `caller.reset_keys`.
- `ModelCaller.call` keeps: building the client, streaming, `on_text`, usage, tool-call indexes and the `BAD_GENERATION` corrective retry (`MAX_REJECTED_RETRIES`; an outer loop around `failover.call` that appends the corrective message). It loses `MAX_ATTEMPTS`, `TRANSIENT_BACKOFFS`, key rotation and the `sleep` argument (existing tests move to `FakeClock`).
- `attempt(key_index)` builds the model with that key (`None` is the keyless build) and lets the raw exception out. Once any text has streamed in the call, it wraps whatever a provider call raised in `StreamedTextFailure(cause)`. The `on_text` wrapper (`_OnTextCallbackError`) is unchanged and never reaches the failover as a provider error.
- Endings, wording from `failover.describe_keys`:
  - `AllKeysExhausted` before any text: raise `ModelKeysExhausted` (a `TurnFailed` with `error_kind = "keys_exhausted"`; every current caller fails as before until task 3 catches it in the model node; compaction never does, see task 3). Text `"<provider>: no usable key for <model>. " + describe_keys(...)`.
  - `StreamedTextFailure`: classify `cause` with the key values, raise `TurnFailed` whose text is the cause's message plus `describe_keys(failover.states(...))`, with `error_kind = "keys_exhausted"` when the cause was a rate limit, billing or auth.
  - When every key is `billing` or `rejected` (none daily): also `providers.mark_needs_attention(provider_id, message)`, as today. The mark only informs (the AI providers page shows it); it never refuses a turn, because spec §2's path for exhausted keys is the stall card with Try again, and a refused turn would never reach the model, so the cooldown probe could not run. So `agents/settings.py` no longer returns `provider_needs_attention` (neither `resolve` nor the agent settings view treats the mark as a model problem; the AI providers page already shows the provider's status and detail; the tests in `tests/agents/test_settings.py` that expect the refusal change to expect none), and the mark is cleared (`ProvidersService.clear_attention(provider_id)`, status `ok`) by `ModelCaller.reset_keys` (Try again), by any successful call of that provider (the caller already reads the provider per call; it writes only when the status is `needs_attention`), and as today by adding a key. Decision #384.
  - A provider with no keys (self-hosted, keyless): `attempt(None)` once, no failover and no key state (task 1). Any error, a 401, 402 or 429 included, is classified and fails the turn with the provider's own message (`TurnFailed(error.message)`, `error_kind` none, so no Try-again card): there is no key to fix or rotate, and the user can send again. It is never marked needs attention. Decision #384.
  - Other provider errors: classify the raw exception with the key values and raise `TurnFailed(error.message)` as today.
- **`error_kind` write path** (the executor's failure path; a change to the I12 `TurnObserver` protocol, so wider than one file):
  - `executor.py`: `TurnOutcome` becomes `tuple[TurnEnd, str | None, ErrorKind | None] | tuple[Literal["waiting"], None, None]`; `_guarded` returns `("failed", err.message, err.error_kind)` for a `TurnFailed`, and every other `return (status, error)` in the file gains `None`. `ErrorKind` is contracts' type (task 1), imported from `contract.py`.
  - `waiting.py`: `_ending_of` (the only unpack site; `worker.py` calls it and constructs no ending) reads the third item; `_Ending` gains `error_kind`, as does `_UNEXPECTED_FAILURE` (none).
  - `turn_end.py`: `store.set_turn(..., error_kind=...)` (`store/turns.py` writes `turns.error_kind`) and `hub.turn_ended(..., error_kind=...)`, which fills contracts' `TurnEndedData.error_kind`.
  - `TurnObserver.turn_ended(project_id, agent, turn_id, status, final_text, error_kind)` in `hooks.py` gains the parameter; its three implementers (`events/observer.py`, `agents/messaging/inbound.py`, `agents/pr_sync.py`) and the test fakes accept it, and `events/observer.py` puts it in the `turn.ended` event payload. A turn that does not fail has none. `MessageView.error_kind` (contracts' field) has no column of its own: the message store (`store/messages.py`, where `MessageView` rows are built) fills it at read time from `turns.error_kind` joined on `turn_id`. To have it on the live update, `record_failure` takes the `error_kind` and writes it with a dedicated `set_error_kind(project_id, turn_id, error_kind)` in `store/turns.py` that touches only that column (never `set_turn`, whose status change stays in `turn_end.py`'s end sequence), before it re-reads and publishes the agent message, so the `message.updated` already carries it; `turn_end.py`'s `set_turn(..., error_kind=...)` writes the same value again with the status.
- Fake OpenAI server, new functions of their own: `FakeState.script_for_key(key, outcomes)`, consumed per request by the `Authorization` bearer key. Outcomes: `"ok"`, `("429", retry_after | None)`, `"429-quota-wording"` (message "You exceeded your current quota", code `rate_limit_exceeded`), `"402"`, `"insufficient_quota"` (429), `"401"`. An empty script keeps the present behavior.
- After the route exists, write the contract with `uv run python -m mlops_agents.api.openapi_export` and commit `frontend/openapi.json` only (`schema.d.ts` is gitignored; `npm run gen:api` only writes it locally).

**Tests (stop condition):**
- the failed agent message's `MessageView` carries `error_kind` on the live `message.updated` and after a reload of the session view (the live one is the case a late write would miss);
- fake server, two keys, `FakeClock`, a real `KeyFailover`: a 429 on key 0 is answered by key 1 in the same call, no sleep; a per-minute limit on every key waits once and the call then succeeds; a "quota exceeded" 429 behaves the same; 402, `insufficient_quota` and 401 exhaust a key at once and the call succeeds on the next;
- every key exhausted before text raises `ModelKeysExhausted` with the per-key text and no key value; a 429 after text raises `TurnFailed` with the explanation and does not retry on another key;
- a turn that fails with every key exhausted ends `failed` with `turns.error_kind = "keys_exhausted"` in the database, in the `turn.ended` event and in the turn view; any other failure leaves it null;
- the `BAD_GENERATION` retry limit and message are unchanged; transient errors retry twice on the same key;
- two parallel calls on one failover share state; state is per model;
- `PUT /providers/{name}/settings` round-trips, rejects 0 and 1441 (422), 404 for an unknown provider; adding or removing a key clears the failover state;
- Decision #384: a provider marked needs attention still starts a turn (no `provider_needs_attention` refusal), which reaches the model; a successful call clears the mark, and so does `reset_keys` (Try again); a call that does not succeed leaves it; a keyless provider answering 401, 402 or 429 fails the turn with the provider's message, `error_kind` null, and is not marked;
- the existing suite passes (updated rotation tests included); `uv run python -m mlops_agents.api.openapi_export --check` passes; checks as task 1; CI green.

---

### Task 3: The stall in the turn graph

**Files:** new `agents/runtime/stall.py`, `agents/runtime/graph.py`, `graph_run.py`, `waiting.py`, `sessions.py` (its stall lines: the `TurnHooks` wiring) and `api/services.py` (its one binding line); `tool_step.py` is not touched; tests: new `tests/agents/runtime/test_stall.py` and `test_stall_restart.py`.

**Interfaces:** Consumes task 2 and I13: `Approvals.create_stall(project_id, turn_id, step, preview) -> ApprovalView`, `ApprovalPart.kind` and `step`, the stall preview, events `turn.stalled` and `turn.resumed`, and the stall's decision (approve resumes with no audit record; reject ends the turn `stopped`), all contracts'. Produces `TurnHooks.approvals: Approvals` and `TurnHooks.events: EventLog` (I21), `StallState` in `TurnState`, and the behavior: the model node stalls, the graph routes `model -> wait -> model`.

**Implementation notes:**
- `stall.py` holds `StallState` (frozen dataclass: `approval_id`, `provider_id`, `model`, `step`, `preview: Preview`, `resolved: bool = False`), the preview builder (`Preview(kind="text", title="Stalled: no usable key", body=exhausted.description)`), the two functions the graph calls (`record_stall`, `await_stall`) and `LateApprovals`. `TurnState` gains `stall: NotRequired[StallState | None]`; `StallState` joins the list in `turn_checkpointer` (its docstring says every added state type is listed there). `TurnHooks` gains `approvals: Approvals` and `events: EventLog`.
- **Construction cycle:** `ApprovalsService` takes `sessions`, and the turn hooks (built in `SessionManager.__init__`, `sessions.py`, the only `TurnHooks(...)` call) need `Approvals`. `stall.py` defines `LateApprovals`, an `Approvals` that delegates `create_stall` to the instance given to `bind(approvals)` and raises if unbound; `SessionManager` builds the hooks with one and exposes `bind_approvals(approvals)`, which `build_services` calls right after it builds `ApprovalsService` (the pattern of `inbound.bind(sessions)`). `events` is the `event_log` `SessionManager` already receives (a `None` log: no event, as elsewhere); it carries `turn.stalled` and `turn.resumed`.
- Model node: catch `ModelKeysExhausted` around `caller.call` (only without streamed text; with text the caller raises a plain `TurnFailed` and the turn fails as today). On it: `approvals.create_stall(project_id, turn_id, step_count, preview)` (idempotent, so a re-run of the node after a restart finds the pending one), record `turn.stalled {turn_id, provider, model}` once per created approval, and return `{"stall": StallState(...)}` with no message and without spending the step budget.
- `route_model`: a pending (unresolved) `stall` routes to `wait` before the tool-call and turn-end checks. The `wait` node becomes a small dispatcher defined in `graph.py`: with a pending `state["stall"]` it calls `await_stall` (`stall.py`), otherwise `step.wait` unchanged. `await_stall` reads the approval through the one `ApprovalStore` the graph already holds for `ToolStep` (contracts task 3 replaces the `ApprovalStore(store.databases)` line in `graph.py` with the shared instance; `await_stall` takes that same object and constructs no store); pending calls `interrupt(None)` with no side effects before it (LangGraph re-runs the node on resume); approved calls `caller.reset_keys(provider_id)`, records `turn.resumed {turn_id}` and returns the state with `stall` marked `resolved`. The edge `wait -> tools` becomes conditional: `model` when `stall` is set (resolved), `tools` otherwise; the model node returns `{"stall": None}` with its next successful step. A crash between the clear and the checkpoint may repeat `turn.resumed`; events are informational, accepted and stated in the PR.
- The card: `graph_run.py` reads `stall` from the interrupted state next to `decisions` and passes it to `handle_turn_waiting` (`waiting.py`), which builds the stall's `ApprovalPart(kind="stall", approval_id, tool_call_id=None, preview, status="pending", note=None, step, proposal_kind=None)` from `StallState` (`approval_id`, `step`, `preview`) and appends it once, by approval id, as it does for tool approvals, so the session and the Inbox show the card. Contracts adds only its `proposal_kind` line in that file. No audit record is written for a stall.
- A stalled turn across a restart is an interrupted graph like any waiting turn: stage 2's recovery restores the card, the resume re-runs `wait`, reads the approval and goes on.
- **Deviation from spec §2, stated in the plan issue and the PR description:** compaction (a model call made inside the turn, outside the model node) does not stall. When its keys run out it raises `ModelKeysExhausted` (nothing in `compaction.py` or its call site in `executor.py` catches it), which reaches `_guarded` and fails the turn `failed` with the per-key text and `error_kind = "keys_exhausted"`; the agent message already exists at that point (`executor.py` adds it before compaction), so `record_failure` completes it and Try again renders on it, instead of pausing on a stall card. A test pins this. Only the model node stalls.
- Embedders (spec §3) never stall: they do not catch `ModelKeysExhausted`; embedders call `failover.call(..., cooldown_minutes=provider.cooldown_minutes, wait=False)` and map `RateLimited` and `AllKeysExhausted` to `EmbedderUnavailable` (embedders' plan).

**Tests (stop condition):** fake server and fake clock, real graph with a checkpointer:
- every key exhausted: the turn goes `waiting`; one `stall` approval with the per-key preview; `turn.stalled` recorded once; no model message; no audit record; step count unchanged;
- Try again: marks cleared, `turn.resumed` recorded, the model called again from the checkpoint, the turn `completed`, earlier messages intact;
- Stop through the card (reject): the approval is `rejected`, the turn `stopped`; Stop through the agent's Stop route: the approval is `cancelled` (by `stop_waiting_turn`), the turn `stopped`; both delete the checkpoint;
- Stop during a rate-limit wait (a `sleep` blocked on the fake clock) cancels the call cleanly and ends the turn `stopped`;
- stall, Try again, exhausted again: a second approval, not a reuse of the decided one; a re-run of the model node with a pending stall finds it (no duplicate);
- restart while stalled (new session manager over the same databases and checkpoint): the card is back, Try again resumes;
- the stall card (`ApprovalPart(kind="stall")`, null `tool_call_id`) appears once in the agent message, the `message.updated` carries it, and a resume that stalls again adds a second card for the new approval, not a duplicate of the first;
- compaction running out of keys fails the turn with `error_kind = "keys_exhausted"` and creates no stall approval;
- text already streamed when the keys run out: the turn ends `failed` with the per-key text and `error_kind = "keys_exhausted"`, no stall;
- a tool approval and a stall in the same turn do not interfere;
- full suite, checks, `uv run python -m mlops_agents.api.openapi_export --check` passes; CI green.

---

### Task 4: The stalled-turn card, Try again on a failed turn, and the cooldown field

**Files:** new `frontend/src/pages/agents/StallCard.tsx` and `StallCard.test.tsx`, `frontend/src/pages/agents/ApprovalCard.tsx` (one line in contracts' dispatcher), `frontend/src/pages/agents/MessageView.tsx` and `MessageList.tsx` (the failed agent message gets the Try again button; `MessageList` passes the send action), `frontend/src/pages/providers/ProviderCard.tsx` (the cooldown field) and its test, `frontend/src/pages/inbox/InboxPage.test.tsx` (a stall case).

**Interfaces:** Consumes task 3, the generated `schema.d.ts` types (`ApprovalPart.kind`, the failed agent message's `error_kind`, `Provider.cooldown_minutes`, the settings route), the dispatcher and `ApprovalControls` of contracts task 3. Produces the card, the Try again button and the field.

**Implementation notes:**
- `StallCard`: title "Stalled: no usable key", the provider and model, one row per key with its state and reason (the preview body rendered readably, never raw), Try again (approve) and Stop (reject) through contracts' `ApprovalControls` and the existing decision call. Settled states read "Resumed" and "Stopped". It renders in the session and in the Inbox through the dispatcher.
- A failed agent message whose `MessageView.error_kind` is `keys_exhausted` offers Try again (the condition is `error_kind === "keys_exhausted"`, never the message status, which is `complete` after a failure), which re-sends the turn's trigger message (the user message of the same `turn_id`) through the existing send call.
- `ProviderCard`: a numeric "Exhausted key cooldown (minutes)" input, 1 to 1440, saved with the settings route, with one sentence on what it does (how long an exhausted key is skipped before one retry).
- shadcn components and error handling as the neighbouring cards; types only from `schema.d.ts`.

**Tests (stop condition):** component tests: the card shows each key's state and reason; Try again and Stop call the right endpoints and disable while pending; the settled states render; the dispatcher picks `StallCard` for `kind: "stall"` and the tool card for `tool`; the Inbox lists a stall with both buttons; a failed turn with `keys_exhausted` shows Try again and re-sends the trigger, other failures do not; the cooldown field saves, validates the range and shows an API error. `npm run lint`, `npm run format:check`, `npm test`, `npm run build`, `npm run e2e` (the existing flow stays green; `flow.spec.ts` is onboarding's, so no case is added); CI green.
