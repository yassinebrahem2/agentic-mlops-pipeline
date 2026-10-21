> **DRAFT. Do not implement.** Still missing: an advisor review round with no blocking issues. The lead removes this banner when the plan leaves `drafts/`.

# Stage 1 — API Sessions Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. Each task is one PR into `main` (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** The routes the chat page uses: read a session, send a message, stop a turn, and follow the session live over server-sent events, with two tabs and reconnects showing the same answer.

**Branch:** `plan/stage-1-api-sessions`. **Consumes:** I1 (agent-runtime: the contract from task 1, `SessionManager` from task 5), I3 (`live_client`, `ServicesDep`, `ProjectDep`), api-resources task 4 (`services.agent_settings`) and task 1 (the projects router's delete route). **Produces:** I7 of the stage overview.

**Read first:** the stage overview (global constraints, review focus 4, "Interfaces between plans" I1, I3, I7), spec sections 5 ("Turns", "Recovery") and 6 (API).

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Session routes and the stream | coding: I1's contract and I3 on `main`; merging: `SessionManager` and api-resources tasks 1 and 4 on `main` |
| 2 | The real runtime behind the routes | task 1 |

Task 1 can be written and tested against a test double as soon as agent-runtime task 1 is on `main`; its PR merges once the runtime's `SessionManager` is there, because it wires it into the app.

---

### Task 1: Session routes and the stream

**Files:**
- Create: `backend/src/mlops_agents/api/routers/sessions.py`, `backend/tests/api/fake_sessions.py` (test-only)
- Modify: `backend/src/mlops_agents/api/services.py` and `app.py` (the `sessions` field and its builder block), `backend/src/mlops_agents/api/routers/projects.py` (one line in the delete route), `frontend/openapi.json`
- Test: `backend/tests/api/test_sessions_routes.py`

**Interfaces:**
- Consumes: I1 (`AgentSessions`, `SessionView`, `MessageView`, `StreamEvent`, `SessionManager` and its constructor), `EventSourceResponse` from `sse-starlette` (3.5), I3's `ServicesDep`, `ProjectDep`, `client`, `live_client`.
- Produces: I7 exactly. Body model `SendMessage {text: str}` (unique name, I3).
  - `GET /projects/{project_id}/agents/{agent}/session` → `SessionView`
  - `POST /projects/{project_id}/agents/{agent}/session/messages` with `SendMessage` → 201 `MessageView`
  - `POST /projects/{project_id}/agents/{agent}/session/stop` → 204
  - `GET /projects/{project_id}/agents/{agent}/session/stream` → `text/event-stream`

**Implementation notes:**
- Wiring: in the lifespan's `AsyncExitStack`, build `SessionManager(databases=..., workspace=..., catalog=..., tools=..., settings=services.agent_settings, providers=..., http=..., git=...)` after the services it uses, and register `stack.push_async_callback(manager.close)`. Field `sessions: AgentSessions` on `Services`.
- `DELETE /projects/{project_id}` (api-resources' projects router) calls `await services.sessions.close_project(project_id)` before `workspace.remove`.
- The stream:
  - The start position: the `Last-Event-ID` header when present (the browser sends it when it reconnects on its own), else the `after` query parameter (the page's first connection, with `SessionView.last_seq`), else none. A value that is not a non-negative integer counts as none. None means `subscribe(..., after_seq=None)`, which yields `resync` first (I1).
  - Each `StreamEvent` becomes one SSE event: `id` = `seq`, `event` = `type`, `data` = `json.dumps(event.data)`.
  - `EventSourceResponse(..., ping=15)` (a comment line every 15 s keeps proxies from closing an idle stream), headers `Cache-Control: no-cache` and `X-Accel-Buffering: no`.
  - When the client goes away, the subscription's iterator is closed (`aclose`), so the runtime drops the subscriber. When the app shuts down, open streams end (sse-starlette's shutdown handling), so a stopping server is never held open by a browser tab.
  - `ProjectDep` checks the project; unknown agent and an agent without chat come back as the runtime's 404 and 409 before the stream starts (call `sessions.get` first, or let `subscribe` raise before the first event and turn that into the JSON error).
- `fake_sessions.py`: `FakeSessions`, implementing `AgentSessions` in memory for route tests only: `get` returns a stored view, `send` appends a message and queues scripted events, `subscribe` yields scripted events (a list the test controls, plus an `asyncio.Queue` for live ones) and records whether it was closed. Tests replace `app.state.services.sessions` with it after the lifespan starts.

**Tests (behaviors to verify)** with `FakeSessions`:
- `GET .../session` returns the fake's view; `POST .../messages` with `{"text": "hi"}` → 201 and the message; an empty body field or an extra field → 422; `POST .../stop` → 204 and the fake recorded it.
- Stream through `live_client` (the in-process client cannot read streams, I3): events arrive with `id`, `event` and `data` as above, in order; `?after=41` reaches `subscribe` as `after_seq=41`; a `Last-Event-ID: 57` header wins over `?after=41`; `Last-Event-ID: abc` → `after_seq=None`; a ping comment arrives when nothing happens for 15 s (patch the interval down to 0.1 s for the test).
- Closing the client's stream closes the fake's iterator (checked within 2 s).
- The live app shuts down within 5 s while a stream is open (`live_client`'s teardown).
- An unknown project → 404 `project_not_found` as JSON, not a stream; the fake raising the runtime's 404 or 409 for the agent → that JSON error.
- `DELETE /projects/{id}` calls `close_project` before the project is removed (the fake records the order through a shared list).
- `anonymous` → 401 on all four routes.

**Checks:** `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`, `uv run python -m mlops_agents.api.openapi_export --check`.

---

### Task 2: The real runtime behind the routes

**Files:**
- Test: `backend/tests/api/test_sessions_live.py`

**Interfaces:**
- Consumes: task 1's routes; the real `SessionManager`; the fake OpenAI-compatible server; api-resources' providers, models, agents and projects routes to set everything up through HTTP.

**Implementation notes:** no product code is expected. If a test here fails because of the runtime or another router, open a Bug issue against that plan's code and say so in the PR; do not patch around it in the routes.

**Tests (behaviors to verify)**, all through `live_client` with the fake model (`fake-tools` and `fake-slow`), set up through the API: a self-hosted provider at the fake's base URL, the model added and verified, made the Orchestrator's default, a local project on a `make_repo` repository.
- Ask the Orchestrator: the stream shows the user's `message.created`, the agent's, `tool.started` and `tool.finished` for Read, `text.delta`s, `message.updated` and `turn.ended` `done`; then `GET .../session` holds both messages, the tool step included.
- Two tabs (review focus 4): two streams on the same session receive identical event lists for a turn. A third client that loads `GET .../session` mid-turn (`fake-slow`) and opens the stream with `?after=<last_seq>` ends with the same final text as the first, with nothing missing or doubled.
- Reconnect: close a stream mid-turn and reopen it with `Last-Event-ID` set to the last id received; the text continues without a gap or duplicate. Reopen with an id from before a restart of the app (a new `live_client` on the same data folder) → the first event is `resync`.
- Stop during `fake-slow` → `turn.ended` with `stopped`, and the next message works.
- `DELETE` the project during a `fake-slow` turn → 204, the stream ends, and the project's folder is gone (this runs on the Windows CI job too).
- No model assigned → `turn.ended` `failed` and a system message saying to choose a model.

**Checks:** as task 1, on both CI systems.

---

## Not in this plan

- The chat page (frontend-chat) and the end-to-end UI test (e2e).
