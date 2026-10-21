> **DRAFT. Do not implement.** Still missing: an advisor review round with no blocking issues. The lead removes this banner when the plan leaves `drafts/`.

# Stage 1 — Agent Runtime Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. Each task is one PR into `main` (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** The one runtime every agent runs on. A user message becomes a turn: the runtime resolves the agent's model, builds its context from the stored messages, runs a small LangGraph loop (model step, tool step, repeat), streams every step to any number of listeners, stores the result, and handles the queue, Stop, step limits, compaction, provider failures and restarts.

**Branch:** `plan/stage-1-agent-runtime`. **Consumes:** merged code and I2 (agent-settings plan). **Produces:** I1 of the stage overview: the contract in task 1, `SessionManager` in task 5.

**Read first:** the stage overview (global constraints, review focus 3 and 4, "Interfaces between plans" I1 and I2), spec sections 2 ("Databases", "Source of truth") and 5 (all of it), and `docs/roadmap.md` ("Agent definitions", "IDs").

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Session contract (I1) | merged code |
| 2 | Message store and context | task 1 |
| 3 | Model caller: streaming, retries, key rotation, usage | task 1, I2 on `main` |
| 4 | The turn: LangGraph loop, executor, compaction | tasks 2 and 3 |
| 5 | Stream hub and `SessionManager`: queue, Stop, recovery | task 4 |

Task 1 is small and merges first: api-sessions and frontend-chat code against it while tasks 2 to 5 are built.

## Decisions this plan makes (inside the runtime)

- **One agent message per turn.** A turn writes one message with `author_kind="agent"`; its `parts` hold, in order, the text, tool calls and tool results the turn produced. The user's message is its own message.
- **Queue.** A message sent while the session is busy is stored with status `queued` and gets its own `queued` turn; turns run one at a time per session, oldest first. When a queued turn starts, its message becomes `complete`.
- **Stop** ends the running turn only (`stopped`); queued turns then run as usual.
- **Restart.** Recovery runs once per project, the first time the manager touches that project after start: `running` turns become `interrupted` (their agent message too); `queued` turns run again in order. A project nobody opens keeps its queued turns until it is opened.
- **Retries** happen only before the first chunk of a model call arrives. A call that fails after streaming started ends the turn `failed`, keeping what streamed.
- **Compaction** runs before a model step, never inside a turn: it summarizes whole earlier turns only.

---

### Task 1: Session contract (I1)

**Files:**
- Create: `backend/src/mlops_agents/agents/runtime/__init__.py` (empty), `contract.py`
- Test: `backend/tests/agents/runtime/__init__.py` (empty), `test_contract.py`

**Produces:** I1 exactly as the overview states, `"resync"` included in `StreamEvent.type`. The module docstring fixes `SessionManager`'s constructor and lifecycle, which task 5 implements:

```python
SessionManager(
    *, databases: Databases, workspace: WorkspaceService, catalog: AgentCatalog,
    tools: ToolRegistry, settings: AgentSettingsService, providers: ProvidersService,
    http: httpx2.AsyncClient, git: GitRunner,
)
async def close(self) -> None   # cancels running turns and marks them interrupted
```
api-sessions builds it in the lifespan's `AsyncExitStack` and registers `close`.

**Tests:** `Part` parses each of the three part shapes from JSON and rejects an unknown `type`; `MessageView` and `SessionView` round-trip through `model_dump_json`/`model_validate_json`; `StreamEvent.type` accepts the seven names and rejects others; a small class implementing the four methods type-checks as `AgentSessions` (pyright strict is the check; the test only instantiates it).

**Checks:** `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`.

---

### Task 2: Message store and context

**Files:**
- Create in `backend/src/mlops_agents/agents/runtime/`: `store.py`, `context.py`
- Test: `backend/tests/agents/runtime/test_store.py`, `test_context.py`

**Interfaces:**
- Consumes: `Databases`, the project.db tables `sessions`, `turns`, `messages`, `llm_calls` (`storage/tables.py`); `new_id`, `now_iso`; task 1's types; `AgentDefinition`; `Project`; `FileAccess`.
- Produces:
  - `SessionStore(databases: Databases)`, every method taking `project_id` first: `session_id(agent) -> str` (get or create), `add_message(session_id, *, author_kind, author_name, parts, status, turn_id=None) -> MessageView`, `update_message(message_id, *, parts=None, status=None) -> MessageView`, `messages(session_id, *, after: str | None = None) -> list[MessageView]` (oldest first, `seq` order), `add_turn(session_id, trigger_message_id, status) -> str`, `set_turn(turn_id, status, *, error=None) -> None` (sets `started_at` on `running`, `ended_at` on an end status), `next_queued_turn(session_id) -> tuple[str, str] | None` (turn id, trigger message id), `active_turn(session_id) -> str | None` (running or queued), `recover() -> None` (the restart rule above, for the whole project database), `set_summary(session_id, summary, compacted_through) -> None`, `summary(session_id) -> tuple[str | None, str | None]`, `add_llm_call(turn_id, *, agent, model_address, prompt_tokens, completion_tokens, latency_ms) -> None`.
  - `context.py`: `build_messages(definition, project, layout, summary, history) -> list[BaseMessage]` (langchain-core messages) and `top_level_layout(files: FileAccess) -> str`.

**Implementation notes:**
- `parts` are stored as the JSON of `list[Part]`; reading validates them back into `Part` objects.
- `build_messages`:
  - one `SystemMessage`: the definition's prompt, then a "Project" block (name, source, default branch, the top-level layout), then, when a summary exists, "Summary of the earlier conversation:" and the summary;
  - each user message → `HumanMessage` (its text parts joined); for a message from another agent (stage 2, `from_agent` set) prefix "Message from <from_agent>:";
  - an agent message's parts → `AIMessage`s and `ToolMessage`s in order: consecutive text and tool-call parts form one `AIMessage` (`content` = the text, `tool_calls` = the calls with their original ids); each tool result is a `ToolMessage` with its `tool_call_id`;
  - a tool call without a result (a stopped or interrupted turn) gets a synthetic `ToolMessage`: "cancelled by the user" for a `stopped` message, "interrupted" otherwise, so the history stays valid for OpenAI-compatible APIs;
  - messages with status `queued` are left out (they are not sent yet).
- `top_level_layout`: the sorted top-level names of the tracked files (folders with a trailing `/`), capped at 100 entries with a "(and N more)" line.

**Tests (behaviors to verify):** the store against a real project database (`databases.project("prj_test")`): `session_id` is stable per agent; messages come back in insertion order with their parts intact (all three part types, unicode text); `update_message` changes parts and status; queued turns come back oldest first; `recover()` turns `running` into `interrupted` (message too) and leaves `queued`, `waiting` and ended turns alone; `active_turn` sees running and queued turns only. `build_messages`: the system message holds the prompt, project block, layout and summary; a turn with text, two tool calls and their results becomes one `AIMessage` with two tool calls followed by two `ToolMessage`s with matching ids; a stopped turn's dangling call gets "cancelled by the user", an interrupted one "interrupted"; queued messages are left out; the result passes `langchain_core`'s own message validation (convert with `convert_to_openai_messages` and check every tool call has a matching tool message).

**Checks:** as task 1.

---

### Task 3: Model caller

**Files:**
- Create: `backend/src/mlops_agents/agents/runtime/model_caller.py`
- Modify: `backend/tests/fakes/fake_openai.py` (the `drop_after_chunks` flag below)
- Test: `backend/tests/agents/runtime/test_model_caller.py`

**Interfaces:**
- Consumes: `ResolvedModel` (with `build(http, key_index)`), `ProvidersService.mark_needs_attention`, `classify`, `ProviderCallError`, `ProviderErrorKind`, `SessionStore.add_llm_call`; the app's `httpx2.AsyncClient`.
- Produces:
  ```python
  @dataclass(frozen=True)
  class ModelReply:
      message: AIMessage                 # text and tool calls, tool-call ids as the provider sent them
      prompt_tokens: int | None
      completion_tokens: int | None

  class ModelCaller:
      def __init__(self, providers: ProvidersService, http: httpx2.AsyncClient,
                   *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None: ...
      async def call(self, model: ResolvedModel, messages: list[BaseMessage],
                     tools: list[dict[str, Any]], on_text: Callable[[str], Awaitable[None]]) -> ModelReply: ...

  class TurnFailed(Exception):            # message is safe to show and is stored as the turn's error
      def __init__(self, message: str) -> None: ...
  ```

**Implementation notes:**
- Stream with `model.build(http, key_index).bind_tools(tools).astream(messages)`, adding chunks together (`AIMessageChunk` addition merges split tool-call arguments); call `on_text` with each text delta as it arrives. Usage comes from the final chunk's `usage_metadata` when the provider sends it, otherwise None (spec: some providers report none while streaming).
- Failures, through `classify(exc, secrets=model.keys)`:
  - `RATE_LIMIT`: with more keys, move to the next key (round-robin from the current one) and retry at once; when every key has hit a rate limit in this call, wait `retry_after` (default 2 s, at most 30 s) through `sleep` and start over. At most 6 attempts in all.
  - `TRANSIENT` and `UNREACHABLE`: retry after 1 s, then 2 s; then fail.
  - `AUTH` and `QUOTA`: `mark_needs_attention(provider_id, message)`; raise `TurnFailed("<provider>: <message>. Fix the key in AI providers.")`. No retry.
  - `BAD_REQUEST` and `UNKNOWN`: raise `TurnFailed(message)`.
  - Retries only while nothing has streamed for this call; after the first chunk, any failure raises `TurnFailed` at once.
- `ModelCaller` writes no `llm_calls` row; the executor (task 4) does, with the reply's usage and its own timing, so a row exists for every completed call.

**Tests (behaviors to verify), against the fake OpenAI-compatible server (`fake_openai` fixture) with a real `ProvidersService` and models added and verified through it; `sleep` replaced by a recorder:**
- A plain reply streams its words to `on_text` in order and returns the full text with usage 100/10; with `usage_in_stream = False` the tokens are None.
- A tool call split across chunks (`fake-tools`) comes back as one tool call with the full arguments and the provider's id.
- `rate_limit_remaining = 1` with two keys: the second attempt uses the second key (check the `Authorization` header the fake recorded) and no sleep happens; with one key: one sleep of the server's `retry-after` (0, so the default 2 s), then success; with the limit never clearing: `TurnFailed` after 6 attempts.
- `server_error_remaining = 2` → sleeps 1 and 2, then success; 3 → `TurnFailed`.
- `auth_fail = True` → `TurnFailed` naming the provider, and the provider is now `needs_attention`; the key appears in neither the message nor the provider's `status_detail`.
- An error after streaming started (make the fake stop mid-stream with a test-only flag added to `FakeState`, `drop_after_chunks: int | None`) → `TurnFailed` at once, no retry, and `on_text` has received the partial text.

**Checks:** as task 1.

---

### Task 4: The turn

**Files:**
- Create in `backend/src/mlops_agents/agents/runtime/`: `graph.py`, `executor.py`, `compaction.py`
- Test: `backend/tests/agents/runtime/test_graph.py`, `test_executor.py`, `test_compaction.py`

**Interfaces:**
- Consumes: tasks 2 and 3; `AgentSettingsService.resolve` (I2); `AgentCatalog`, `ToolRegistry` (`schemas`, `get`), `ToolContext`, `RepoFiles`, `GitRunner`; `WorkspaceService.get`, `repo_root`; `connect_checkpointer` (`storage/sqlite.py`) and `AsyncSqliteSaver`.
- Produces:
  ```python
  class TurnSink(Protocol):           # task 5's hub implements it
      async def text(self, message_id: str, text: str) -> None: ...
      async def tool_started(self, message_id: str, call: ToolCallPart) -> None: ...
      async def tool_finished(self, message_id: str, result: ToolResultPart) -> None: ...
      async def message(self, message: MessageView, *, created: bool) -> None: ...

  class TurnExecutor:
      def __init__(self, *, store: SessionStore, caller: ModelCaller, settings: AgentSettingsService,
                   catalog: AgentCatalog, tools: ToolRegistry, workspace: WorkspaceService,
                   git: GitRunner) -> None: ...
      async def run(self, project_id: str, session_id: str, agent: str, turn_id: str,
                    sink: TurnSink) -> tuple[TurnEnd, str | None]: ...   # end status and error text
      def live(self, turn_id: str) -> MessageView | None: ...           # the agent message so far
  ```

**Implementation notes:**
- `run`:
  1. Project checks: status `missing` or `clone_failed` → end `failed` with the project's `status_detail`; status `empty` → write the agent message "This repository has no committed files yet. Commit your files, then ask again." with no model call and end `done`.
  2. `settings.resolve(project_id, agent)`; a `DomainError` ends the turn `failed` with its message (the user reads "choose a model ..." in the session).
  3. Create the agent message (status `streaming`, no parts) and announce it with `sink.message(..., created=True)`; keep it in memory for `live`.
  4. Compaction (below), then the graph.
  5. On the end: write the final parts and status (`complete`, `stopped` when cancelled, `interrupted`, or `complete` with the error recorded on the turn for `failed`), `sink.message(..., created=False)`, and return.
- The graph (`graph.py`): our own `StateGraph`, not the prebuilt agent. Nodes `model` and `tools`; `model` → `tools` when the reply has tool calls, else end; `tools` → `model`. State: the langchain messages of this turn and a step count. Before each model step: when `max_steps` is reached, end with the text "Stopped after <n> steps (this agent's step limit). Send a message to continue." added as a text part. Compiled with an `AsyncSqliteSaver` on the project database (connection from `connect_checkpointer`, so it gets the SQLite PRAGMAs), thread id = turn id; the thread is deleted with the checkpointer's own delete call when the turn ends, whatever the outcome.
- Tool step: for each tool call, `sink.tool_started`, then `tool.invoke(ToolContext(files=RepoFiles(root, git)), args)` (one `RepoFiles` per turn: it caches `git ls-files`), `sink.tool_finished`, a `ToolResultPart`. An unknown tool name gives an error result ("There is no tool named X."). Every result, errors included, goes back to the model (spec section 5).
- After each model step and each tool step, store the message's parts so far (`update_message`), so a crash loses at most one step; `llm_calls` gets one row per completed model call with its latency.
- Cancellation (Stop, task 5) arrives as `asyncio.CancelledError` in `run`: catch it at the top of `run`, store the parts so far with status `stopped`, return `("stopped", None)`; a tool call cut off gets no result part (context adds "cancelled by the user").
- `compaction.py`: `async def compact_if_needed(store, caller, model, project_id, session_id, history, system_text) -> None`. Size = the prompt tokens reported for the session's last model call (if any) plus `len(text) / 4` for every message after it; when there is no reported usage the estimate covers everything. Above 75% of `context_window`: summarize the oldest whole turns, keeping the last two turns and never splitting a tool call from its result, with one model call (no tools) asking for a summary of decisions, facts learned about the repository and open questions; `set_summary` with the new summary (the old summary is part of the input) and `compacted_through` = the last summarized message's id.

**Tests (behaviors to verify)**, with the fake server, real stores and a repo from `make_repo`; a `RecordingSink` collects events:
- `fake-tools` on the Orchestrator: the turn calls Read on `README.md`, then answers "Done. First line: <first line>"; the stored agent message has text-less tool call, tool result and final text in that order; the sink saw `message(created=True)`, `tool_started`, `tool_finished`, the text deltas and `message(created=False)`; one `llm_calls` row per model call (2) with the fake's usage.
- A tracked secret (`.env`) and an untracked file are not readable through the turn (the tool result is an error the model sees); the turn still ends `done`.
- `max_steps = 1` (project override through `AgentSettingsService`) ends after one model step with the step-limit text; `None` runs to the end.
- No model assigned → ends `failed` with I2's `model_unassigned` message and no model call; project `empty` → the fixed message, no model call; project `missing` → `failed` with its detail.
- Cancelling the task while a slow test tool runs (a test-only tool registered in a test registry that sleeps until cancelled, used by a throwaway definition in `tmp_path`) → returns `stopped`, the message is `stopped`, and a new turn on the same session then runs and its context holds "cancelled by the user" for that call.
- `TurnFailed` from the caller → `failed` with its message, partial text kept.
- The checkpointer thread for the turn is gone after `done`, `stopped` and `failed`.
- Compaction: with the model's context window set small (`set_context_window`), the third turn triggers a summary; `summary()` returns it with `compacted_through` on a turn boundary; the next context starts with the summary and the last two turns; without reported usage (`usage_in_stream = False`) the estimate alone triggers it.
- A second agent defined only by a definition file in `tmp_path` runs a turn with no code change (spec "Done when").

**Checks:** as task 1.

---

### Task 5: Stream hub and `SessionManager`

**Files:**
- Create in `backend/src/mlops_agents/agents/runtime/`: `hub.py`, `sessions.py`
- Test: `backend/tests/agents/runtime/test_hub.py`, `test_sessions.py`

**Interfaces:**
- Consumes: tasks 2 to 4; the constructor fixed in task 1.
- Produces: `SessionManager`, implementing `AgentSessions` (I1) with `close()`.

**Implementation notes:**
- `hub.py`: one `SessionHub` per session, in memory, implementing `TurnSink`. Each event gets the session's next `seq` (from 1, per process). It keeps the last 5000 events in a ring buffer and fans them out to subscribers through one `asyncio.Queue` each. `subscribe(after_seq)`: when `after_seq` is None, below the oldest buffered seq minus one, or above the current seq, yield one `resync` event first, then live events; otherwise yield the buffered events after `after_seq`, then live ones, with no gap and no duplicate (take the buffer snapshot and register the queue without an `await` in between). A subscriber that cannot keep up (queue above 10 000 events) gets `resync` and its backlog is dropped.
- `sessions.py`, `SessionManager`:
  - `get`: recovery for the project if not done yet; checks the agent exists (404) and has `chat` (409); returns the stored messages with the running turn's message replaced by `TurnExecutor.live` (the text and tool steps so far), `busy`, and the hub's current seq as `last_seq` (read in the same step as the live message, so events after it are exactly what came later).
  - `send`: empty text → 422 `message_empty`; stores the user message (`complete` when idle, `queued` when busy), creates its turn, publishes `message.created`, starts the worker for the session when idle, returns the message.
  - One worker task per busy session: runs queued turns in order through `TurnExecutor.run`, sets each turn's status, publishes `turn.ended`, and exits when the queue is empty.
  - `stop`: cancels the worker's current `run` only (queued turns continue); a no-op when idle.
  - `close`: cancels every worker; their running turns end `interrupted`.
- Tool steps and text are published through the hub as `tool.started`, `tool.finished`, `text.delta`; messages as `message.created` / `message.updated`.

**Tests (behaviors to verify)**, with the fake server and a real stack (`SessionManager` built as api-sessions will build it):
- Ask the Orchestrator (`fake-tools`): `send` returns the user message; a subscriber sees `message.created` (user), `message.created` (agent), `tool.started`, `tool.finished`, `text.delta`s, `message.updated`, `turn.ended` with `done`, with strictly increasing seqs.
- Two subscribers on the same session receive identical event lists. A subscriber that joins mid-turn after `get` (using `fake-slow`) ends with the same final text as the first: the text from `get`'s live message plus the deltas after `last_seq`, nothing missing or doubled (review focus 4).
- `subscribe(after_seq=0)` after 6000 events, or with a seq above the current one, yields `resync` first.
- Three messages sent at once: the second and third are `queued`, run in order, each gets its own `turn.ended`; `busy` is true until the last ends.
- Stop during `fake-slow` → `turn.ended` `stopped`, the partial text stays in the stored message, a queued message then runs; Stop during a slow tool, then a new message: the session keeps working (spec section 8).
- Recovery: a project database with a `running` turn and a `queued` turn written by a previous manager → the first `get` marks the running one `interrupted` and the queued one runs.
- `close()` during a turn → the turn is `interrupted`.
- An agent with `chat: false` (test definition) → `send` 409; unknown agent → 404; empty text → 422.

**Checks:** as task 1. Also `uv run pytest -m live` once with a real Groq or Gemini key on a verified model: ask the Orchestrator what the repository's README says and report the answer in the PR.

---

## Not in this plan

- The HTTP routes and the SSE endpoint (api-sessions), the chat page (frontend-chat).
- Approvals and the `waiting` status (stage 2): `recover()` leaves `waiting` turns alone so stage 2 can use them.
