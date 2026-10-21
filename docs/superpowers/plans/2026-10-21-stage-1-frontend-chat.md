# Stage 1 — Frontend Chat Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** The Agents page: a project's session with the Orchestrator (and any other chat agent), streamed answers, collapsible tool steps, queued messages, Stop, stopped and interrupted turns marked, failure notices, and a view that stays right across two tabs and reconnects.

**Branch:** `plan/stage-1-frontend-chat`. **Consumes:** I5 (frontend-foundation tasks 1 and 2), I7 (api-sessions), I1's shapes through the contract, I6 `GET /projects/{id}/agents`. **Produces:** the page at `/projects/:projectId/agents` (I5's page paths).

**Read first:** the stage overview (global constraints, review focus 4, I1, I5, I7, the stage 1 end-to-end flow steps 4 and 5), spec sections 5 ("Turns", "Recovery") and 6, and the frontend-workspace plan's "Rules for every page", which apply here too.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Session state and the stream | coding: I5 on `main` (I1 and I7's shapes are fixed in the overview); merging: api-sessions task 1 (#55) on `main` |
| 2 | The Agents page | task 1; api-resources task 4 (#53); frontend-workspace task 1 (#58, `ProjectStatus`) |

Task 1 can be written as soon as frontend-foundation task 2 is on `main`: the event and session shapes are fixed in I1, and its tests use a fake stream. Its code type-checks against the generated types once api-sessions task 1 is on `main`; the plan PR comes after task 2 anyway.

---

### Task 1: Session state and the stream

**Files:**
- Create: `frontend/src/pages/agents/session/events.ts`, `applyEvent.ts`, `useSession.ts`, `stream.ts`
- Test: `applyEvent.test.ts`, `useSession.test.tsx`

**Interfaces:**
- Consumes: I7 (`GET .../session` → `SessionView`, `POST .../session/messages`, `POST .../session/stop`, `GET .../session/stream`), I1's `SessionView`, `MessageView` and `Part` (from the generated schema), `api`, `unwrap`, `ApiError`. The stream's event shapes are not in the OpenAPI contract (FastAPI does not describe SSE events), so `events.ts` writes them by hand from I1.
- Produces:
  ```ts
  // events.ts: the seven I1 event data types and a type guard per event name
  export type StreamEventData = { type: "message.created"; message: MessageView } | ... ;
  export function parseEvent(type: string, data: string): StreamEventData | null;  // JSON.parse as unknown, then narrowed
  type SessionState = { messages: MessageView[]; busy: boolean; lastSeq: number };
  export function applyEvent(state: SessionState, event: { seq: number; type: string; data: unknown }): SessionState;
  export function useSession(projectId: string, agent: string, options?: { openStream?: (url: string) => StreamLike }): {
    state: SessionState | undefined; error: ApiError | undefined; connection: "live" | "reconnecting" | "closed";
    send(text: string): Promise<void>; stop(): Promise<void>;
  };
  // stream.ts
  export type StreamLike = Pick<EventSource, "addEventListener" | "close" | "readyState"> & { onerror: ((e: Event) => void) | null };
  ```

**Implementation notes:**
- `parseEvent` parses `data` as `unknown` and narrows it with a guard for its event name (no casts: ESLint strict forbids `any`). An event that fails its guard is ignored and counted as a stream error; it never crashes the page.
- `applyEvent` is a pure function that folds events the way the runtime's hub does (I1):
  - `message.created`: append the message unless one with that id exists;
  - `text.delta`: append the text to the last text part of that message, or add a text part when the last part is a tool call or result;
  - `tool.started`: add a `tool_call` part; `tool.finished`: add a `tool_result` part;
  - `message.updated`: replace the message with the event's (its final state wins over anything folded before);
  - `turn.ended`: `busy` is false unless a queued message remains in `messages`;
  - an event whose `seq` is not above `lastSeq` is ignored (no duplicates after a reconnect); every applied event sets `lastSeq`.
  `busy` becomes true on `message.created` for a user message.
- `useSession`:
  1. load `GET .../session` (query key `["session", projectId, agent]`) into `SessionState`;
  2. open the stream at `.../session/stream?after=<last_seq>` with `openStream` (default `new EventSource(url)`, same origin so the session cookie goes along); listen for each event name of I1 and feed `applyEvent`;
  3. on `resync`: close the stream, reload the session, reopen with the new `last_seq`. A reload always replaces the state (messages, `busy`, `lastSeq`) and never merges into it: a hub created again after a restart or `close_project` has no relation to the old seq;
  4. on an error, branch on `readyState`:
     - `CONNECTING`: the browser is retrying a dropped connection and will send `Last-Event-ID`; set `connection: "reconnecting"` and wait for the next event;
     - `CLOSED`: the server refused the stream (EventSource does not retry after an HTTP error such as 404 or 401), so reload the session at once: a 404 (the project or the agent is gone) closes the stream for good (`connection: "closed"`, `error` set) and nothing retries; a 401 goes through the query cache's sign-in handling (I5); anything else reopens after a backoff (1 s, 2 s, 5 s, then every 10 s);
  5. close the stream when the component unmounts or the project or agent changes.
- `send(text)`: `POST .../messages`; the message itself arrives through `message.created` (no optimistic copy, so two tabs and the sender see the same thing); errors are returned for the page to show. `stop()`: `POST .../stop`.

**Tests (behaviors to verify):**
- `applyEvent` on a recorded turn (user message, agent message created, tool started and finished, deltas, updated, ended) gives the same messages as the final `message.updated` and the same as `GET .../session` would after the turn; a replayed event (same seq) changes nothing; deltas after a tool result start a new text part; `busy` follows queued messages.
- `parseEvent` accepts each of the seven shapes and returns null for malformed JSON or a wrong shape.
- After a queued turn's `message.updated` (its user message now `complete`) and its `turn.ended`, `busy` is false.
- `useSession` with a fake `StreamLike` and a stubbed `fetch`: it opens the stream with `?after=<last_seq>` from the loaded session; events update `state`; `resync` closes the stream, reloads, replaces the state and reopens with the new seq; an error with `readyState` `CONNECTING` shows "reconnecting" and makes no request; an error with `readyState` `CLOSED` reloads at once, and a 404 then closes it for good with the error set; another failure reopens after the backoff (fake timers); unmounting closes the stream.

**Checks (from `frontend/`):** `npm run lint`, `npm run format:check`, `npm test`, `npm run build`.

---

### Task 2: The Agents page

**Files:**
- Create in `frontend/src/pages/agents/`: `route.tsx`, `AgentsPage.tsx`, `MessageList.tsx`, `MessageView.tsx`, `ToolStep.tsx`, `Composer.tsx`
- Test: `AgentsPage.test.tsx`, `MessageView.test.tsx`

**Interfaces:**
- Consumes: task 1; I6 `GET /projects/{id}/agents` (`AgentInfo` with `chat` and `settings.model_problem`), `GET /projects/{id}` (status); `ProjectStatus` (`src/components/`, frontend-workspace); `react-markdown` with `remark-gfm` (already dependencies).
- Produces: `route` at `/projects/:projectId/agents` with `nav: { area: "project", label: "Agents", order: 0 }`.

**Implementation notes:**
- Agents list: the project's chat agents (`chat: true`); stage 1 has the Orchestrator only, so the list shows when there is more than one. The selected agent is the `agent` query parameter, defaulting to the first chat agent.
- Above the conversation: `ProjectStatus` when the project is not `ok`, and the agent's `settings.model_problem` as a warning with links to "AI providers" (`/providers`) and "Project settings" (`/projects/:projectId/settings`).
- Messages, oldest first:
  - user messages on one side, with "Queued" while their status is `queued`;
  - agent messages: text rendered as markdown (`react-markdown` renders no raw HTML, which keeps model output from injecting markup), tool steps as `ToolStep`, and a mark for `stopped` ("Stopped") and `interrupted` ("Interrupted: the app stopped during this answer");
  - `author_kind: "system"` messages as notices across the column, not as chat bubbles (for example the reason a turn failed).
  - A message still streaming shows a typing indicator after its last part.
- `ToolStep`: a collapsible item, closed by default, labelled with the tool name and a one-line summary of its input (for Read the file path, for Grep the pattern, for Glob the pattern); open, it shows the input and the result (monospace, scrollable, errors styled as errors). While the result has not arrived it shows "Running".
- Composer: a labelled text area ("Message <agent display name>"), Enter sends, Shift+Enter adds a line; "Send" is disabled for empty text; while `busy` the page shows "Stop" (sending still works and queues). Send errors show under the composer.
- The list keeps the newest content in view while the user is at the bottom, and does not move when they have scrolled up.
- `connection: "reconnecting"` shows a small "Reconnecting..." note; `"closed"` with an error shows `errorText(error)` and a link to Projects.

**Tests (behaviors to verify)** (task 1's hook with a fake stream, `renderPage` at `/projects/prj_1/agents`):
- A recorded turn renders the user message, a closed tool step for Read with the file path, and the markdown answer; opening the step shows the input and the result.
- Model output is untrusted (the model reads repository files that may hold injected text): a message with a raw `<script>` or `<img onerror>` renders as text, never as an element, and `[x](javascript:alert(1))` renders no `javascript:` href (react-markdown's default `urlTransform`; the test keeps it that way).
- Queued, stopped and interrupted marks; a system message renders as a notice.
- Sending: Enter sends, Shift+Enter does not, empty text cannot be sent; while busy, "Stop" sends the stop request.
- `model_problem` and a `missing` project status are shown with their links.
- "Reconnecting..." while the stream reconnects; a closed stream with a 404 shows the message.

**Checks:** as task 1. Also by hand with the backend and `npm run dev` against the fake provider or a real one: ask the Orchestrator about the repository in two tabs; both show the same answer; reload in the middle of an answer and it continues.

---

## Not in this plan

- Other agents' sessions and agent-to-agent messages (stage 2); the end-to-end test (e2e plan).
