# Stage 2: Gemini Tool Calls and OpenPullRequest Fixes Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. Two tasks, one PR for the branch, no early PR (`AGENTS.md` rule 5). Each task lists its files, tests (its stop condition) and checks.

**Goal:** bugs #610 and #611. Multi-turn tool use works on Gemini 3 (its tool-call signature is kept and sent back), and `OpenPullRequest` returns a tool error instead of ending the turn when the sandbox is down or `git add` fails.

**Branch:** `plan/stage-2-gemini-and-pr-fixes`. **Consumes:** merged code. **Produces:** nothing other plans use; `ToolCallPart` gains one optional field (task 1), everything else keeps its signature. The plan PR says `Closes` the plan issue, its task issues, #610 and #611.

**Read first:** bugs #610 and #611 (each lists where it fails, with file and line); `backend/src/mlops_agents/providers/bridge.py`; `agents/runtime/model_caller.py`, `graph.py` (~270-295), `context.py` (~110-145), `contract.py` (`ToolCallPart`); `agents/tools/pull_request.py`, `agents/tools/bash.py` (how it handles `SandboxUnavailable`), `agents/tasks/commit.py`; `backend/tests/fakes/fake_openai.py`.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | A tool call's provider data goes back with it (Gemini's thought signature) | merged code |
| 2 | `OpenPullRequest` never ends the turn on a sandbox or `git add` failure | merged code |

---

### Task 1: A tool call's provider data goes back with it (#610)

**Files:** `backend/src/mlops_agents/providers/bridge.py` (the app's one chat-model class); `agents/runtime/model_caller.py`, `graph.py`, `context.py` and `contract.py` (`ToolCallPart`); `backend/tests/fakes/fake_openai.py` (a scripted reply whose tool-call delta carries `extra_content`, and the recorded request bodies); tests under `backend/tests/providers/` and `backend/tests/agents/`.

**Notes:**
- **Opaque, per call.** Gemini 3 returns `tool_calls[i].extra_content` (holding `google.thought_signature`) and needs it back unchanged on that same call in every later request. The app never reads inside it: it keeps the whole `extra_content` object of each tool call, keyed by the call's id, and sends it back on the call with that id. Nothing is invented for a call that had none.
- **Where it is lost** (all five, #610): langchain-openai's stream parse, `model_caller`'s rebuild, langchain's request serializer, `ToolCallPart`, and `context.py`'s history rebuild. langchain-openai 1.7.2 has no support for the field, so `build_chat_model` returns a small `ChatOpenAI` subclass that captures it from the stream and puts it back into the request payload. Overriding langchain methods is a third-party boundary: name the methods and the langchain version in a comment, and the task's tests must fail if a langchain upgrade stops the capture or the re-emit (they go through the real class against the fake server, not a mock). Chunk merging concatenates strings in `additional_kwargs`: carry the data so that a value split across deltas, or set on one delta of a call, is merged into one object, never doubled.
- **Stored with the call.** `ToolCallPart` gains one optional field for this object (`None` when the provider sent none); the parts are JSON, so no migration. It is provider data, not something a person reads: it is not shown in the UI.
- **Only to the model that produced it.** A signature can be refused as invalid when the model or route changes, and a stored message does not record its model today: store the model address with it on the part, and send it back only when the request goes to that model. Requests to any other model, or with history that has none, are byte-for-byte what they are today.
- **History without signatures.** When an agent is switched to a Gemini 3 model mid-session, its older tool calls carry no signature. Check Google's documented handling for unsigned history in the OpenAI-compatible endpoint and follow it exactly (a documented placeholder value, if Google gives one, lives in one constant); never make up a signature of our own. If it cannot be handled, say so in the plan issue and list it under "Known debt added".
- **The schema.** `ToolCallPart` also goes out over the live stream (`hub.py`) and the messages API: keep the field out at the API boundary, or regenerate `openapi.json` and the frontend types; either way the UI does not show it.
- Compaction (tool calls become text) and provider verification (one request) need no change.

**Tests (stop condition)**, against the fake OpenAI server through the real chat-model class:
- First, one that fails on `main`: a streamed tool call with `extra_content`; the turn's next model request carries it, unchanged, on that call.
- A later turn, and a turn after a restart (history rebuilt from the store), still send it on that call.
- Two tool calls in one reply, only one with `extra_content`: only that one carries it back.
- A value split across two deltas of one call is sent back as one object, not doubled.
- A provider that sends none: the request is unchanged (no new key). History from one model sent to another: no `extra_content`.
- Backend suite, ruff, pyright; CI green.

---

### Task 2: `OpenPullRequest` never ends the turn on a sandbox or `git add` failure (#611)

**Files:** `backend/src/mlops_agents/agents/tasks/commit.py` (and `diff.py`'s `get_candidate_paths` if it is on the path); `agents/tools/pull_request.py`; tests under `backend/tests/agents/`.

**Notes:**
- **At the cause.** `commit_task` already turns a failed `git commit` into `CommitRefused`; it does the same for the other git steps of a commit (`git add`, `git status`, the candidate paths), with git's message. `OpenPullRequest` already returns `CommitRefused` as a tool error.
- `SandboxUnavailable` raised by `Tasks.commit` becomes a tool error in both the GitHub and the local case, as `Bash` does. The approval preview (`OpenPullRequestTool.preview` → `Tasks.staged_diff`, also under `pause_task`) raises it too, before `run` starts: it must not end the turn either. No blanket `except Exception`.
- **The reporter's own case.** #611's reporter may have hit a third trigger. If the server-log traceback is attached to #611 by the time you start, reproduce it first; if its cause is not one of these two, note it in the plan issue and stop.

**Tests (stop condition):** on a local project and on one with a GitHub remote (the fake GitHub): the sandbox unavailable during `OpenPullRequest`, in its preview and in its run, gives a tool error and the turn goes on; a nested git repository in the worktree makes `git add` fail, and the agent gets a tool error with git's message, and the turn goes on; the existing `OpenPullRequest` tests stay green; backend suite, ruff, pyright; CI green.

## Known debt added

None planned. Anything accepted while implementing goes here and into `docs/known-debt.md`.
