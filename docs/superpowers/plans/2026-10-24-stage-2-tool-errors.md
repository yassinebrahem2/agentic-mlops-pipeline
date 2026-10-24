# Stage 2 — Tool Errors Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** A tool call that goes wrong is input for the model, never the end of the turn (`docs/roadmap.md`, Stage 2). Today two cases end it:
- A call whose arguments are not valid JSON is dropped, and the turn ends silently as "done" (bug #144).
- A provider that rejects the model's tool call server-side (Groq: "Tool call validation failed ... missing properties: 'pattern'") ends the turn as failed.

Also make the tool schemas clearer, so weaker models make fewer of these mistakes.

**Branch:** `plan/stage-2-tool-errors`. **Consumes:** merged code. **Produces:** nothing other plans use.

**Read first:** bug #144, the stage 2 overview, and the stage 1 spec §5 (Tools) and §7.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Invalid tool-call arguments get an error result (#144) | merged code |
| 2 | A provider's rejection of a tool call goes back to the model | merged code |
| 3 | Clearer tool schemas | merged code |

---

### Task 1: Invalid tool-call arguments get an error result (#144)

**Files:** `backend/src/mlops_agents/agents/runtime/graph.py`, its tests under `backend/tests/agents/runtime/`, `backend/tests/fakes/fake_openai.py` (a scripted model that streams one tool call with invalid JSON arguments, then answers in text on the next request).

**Interfaces:** Consumes merged code. Produces none.

**Cause:** `model_caller.py` keeps the bad call in the `AIMessage`'s `invalid_tool_calls`, but `graph.py` reads only `tool_calls` (in `model_node`, `tools_node` and `route_model`). `route_model` returns END, and the turn ends as "done" with no answer.

**Implementation notes:**
- Treat each entry of `invalid_tool_calls` like a tool call whose tool fails:
  - it gets a `ToolCallPart` (`input: {}`) and a `tool.started` event;
  - in the `AIMessage` kept in the turn's state, it becomes a normal entry of `tool_calls` with `args: {}`, so the history sent back to the provider never carries invalid JSON, and matches what the stored parts rebuild to on the next turn;
  - its result is an error `ToolResultPart`: `The arguments of <name> were not valid JSON: <args>`, with the raw args capped at 500 characters;
  - `route_model` goes to the tools step when either list is non-empty.
- One function builds that result text. Every place that answers an invalid call uses it.
- The request sent to the provider after the error must stay valid: every tool result answers a call in the assistant message before it. Prove this from the request the fake receives.

**Tests (stop condition):**
- A turn whose first model reply has an invalid call shows an error tool result naming the tool. The model is called again and the turn ends `done` with the second reply's text.
- The second request the fake receives has an assistant message with that call's id, followed by a tool message with the same id.
- A reply with one valid and one invalid call runs the valid tool and answers both, in the reply's order.
- Full backend suite, `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`; CI green.

---

### Task 2: A provider's rejection of a tool call goes back to the model

**Files:** `backend/src/mlops_agents/providers/errors.py`, `backend/src/mlops_agents/agents/runtime/model_caller.py`, their tests, `backend/tests/fakes/fake_openai.py` (a scripted model whose first request gets a Groq-style 400 with body `{"error": {"message": "...", "code": "tool_use_failed", "failed_generation": "..."}}` and whose next request answers in text).

**Interfaces:** Consumes merged code. Produces none.

**Cause:** `classify` maps the 400 to `BAD_REQUEST` (or a plain `APIError` from an SSE `error` event to `UNKNOWN`). `ModelCaller.call` then raises `TurnFailed`, and the turn ends as failed.

**Implementation notes:**
- Add `ProviderErrorKind.BAD_GENERATION`. It covers a status error whose body's `code` is `tool_use_failed` or `json_validate_failed`, from either the 400 or the SSE `error` event (both carry the body). Its `message` is the provider's message, scrubbed like every other message.
- In `ModelCaller.call`, a `BAD_GENERATION` is retried when no text delta has reached `on_text` yet. Today `streamed` turns true on any chunk, including a role-only one. The rule becomes "no text sent yet" for every retry kind (rate limit, transient, rejection), one rule in one place. Each attempt already starts from empty accumulators; keep that. It is retried at most twice (`MAX_REJECTED_RETRIES = 2`). The counter is separate from the rate-limit and transient retries.
  - Each retry appends one user-role message to the request, in memory only: `Your last tool call was rejected by the provider: <message>. Call the tool again with every required argument, or answer without it.`
  - That message is not stored and not shown, and the step counts once toward `max_steps`.
- After the bound, or once text has streamed, the turn fails with the provider's message, as today.
- Retries stay inside the caller, like rate-limit retries; `graph.py` still writes one `llm_calls` row per step.

**Tests (stop condition):**
- A first request rejected with `tool_use_failed`: the second request carries the corrective message, and the turn ends `done` with the reply's text. Nothing about the rejection is stored in `messages`.
- After a retry, the final message holds only the retried attempt's tool calls.
- Three rejections in a row: the turn fails with the provider's message, after exactly three requests.
- The same rejection arriving as an SSE `error` event after a role-only chunk (the fake gets that mode) is retried, and the turn ends `done`.
- A different 400 (no such code) still fails the turn on the first request.
- Full backend suite, ruff, format, pyright; CI green.

---

### Task 3: Clearer tool schemas

**Files:** `backend/src/mlops_agents/agents/tools/base.py`, `backend/src/mlops_agents/agents/tools/grep.py`, their tests.

**Interfaces:** Consumes merged code. Produces none.

**Cause:** the model mixed up Grep's `type` (file type) with `output_mode`. `output_mode`'s description starts with "content:", the tool description says "use output_mode=content", and `type`'s description is only a list of extensions. The schema pydantic emits is also noisy for small models: a `title` on every field, and `anyOf [X, null]` on every optional field.

**Implementation notes:**
- Grep wording: `type` says it is a file-type filter, gives an example (`py`), and says it is not the output format. `output_mode` and the tool description name the modes without the `=content` shorthand. Keep Claude Code's argument names.
- `AgentTool.schema()` (the one place schemas are built) removes every `title` key. It turns `anyOf: [X, {"type": "null"}]` on a field with a `null` default into `X`. Nothing else changes, and validation still uses the pydantic model.

**Tests (stop condition):**
- Grep's emitted schema has no `title`, no `anyOf`, and `required == ["pattern"]`. `type`'s description mentions `output_mode`.
- Every registered tool's schema still validates the arguments its existing tests use.
- Full backend suite, ruff, format, pyright; CI green.
