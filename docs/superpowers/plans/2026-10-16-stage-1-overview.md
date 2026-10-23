# Stage 1: Foundation and First Agent — Plans Overview

Stage 1 is built through several plans. This overview holds what they all share. Read it, the spec and `docs/roadmap.md` before starting any plan.

**Goal:** One command installs and starts the app. The user adds AI providers and a project (local folder or GitHub repo), then chats with the Orchestrator, which reads the repo with read-only Read/Glob/Grep tools and streams its answers live. This sits on the shared agent runtime every later agent will use.

**Architecture:** A modular monolith. One FastAPI process (`backend/`) holds domain modules (`storage`, `credentials`, `providers`, `integrations/github`, `workspace`, `agents`) under one `api` layer, and serves a React SPA (`frontend/`) built into the Python wheel. Storage is SQLite only: `app.db` plus one `project.db` per project. Agents run on one LangGraph runtime driven by markdown agent definition files, and answers stream over server-sent events.

**Tech Stack:** Python 3.12, uv, FastAPI, SQLAlchemy Core (async, aiosqlite), LangGraph + langchain-openai, pydantic, keyring, cryptography, sse-starlette, pytest. React 19, Vite 7, TypeScript 5.9, Tailwind 4, shadcn/ui, React Router 7, TanStack Query 5, openapi-typescript + openapi-fetch, Vitest 5, Playwright. GitHub Actions CI.

**Spec:** `docs/superpowers/specs/2026-10-16-stage-1-foundation-design.md`. Shared contracts: `docs/roadmap.md`.

## Plans

Each plan has one owner, who claims it by assigning themselves to its plan issue (`AGENTS.md`, Team workflow). A task can start as soon as everything it consumes is on `main`; the "Starts" column says when that first happens. The interfaces between plans are fixed in "Interfaces between plans" below; changing one takes a Decision issue.

| Plan | Branch | Covers | Consumes (besides merged code) | Starts | State |
|---|---|---|---|---|---|
| backend-core | `plan/stage-1-backend-core` | Backend scaffold and CI, config, IDs, storage, credentials, git runner, shared test fixtures | | | Merged |
| frontend-scaffold | `plan/stage-1-frontend-scaffold` | React app scaffold, tooling, frontend CI | | | Merged |
| providers | `plan/stage-1-providers` | AI providers registry, fake OpenAI-compatible server, model verification, LangChain bridge | | | Merged |
| github-workspace | `plan/stage-1-github-workspace` | GitHub API and auth, local and GitHub projects | | | Merged |
| agent-tools | `plan/stage-1-agent-tools` | Agent definition files, catalog, tool registry, tracked-file filter, Read/Glob/Grep | | | Merged |
| agent-settings | `plan/stage-1-agent-settings` | Default model per agent, per-project model override and limits, resolving the model a turn uses | none | now | Ready: plan issue open |
| agent-runtime | `plan/stage-1-agent-runtime` | Message store, context and compaction, model calls with retries and key rotation, the LangGraph loop, turn executor, stream hub, session queue, Stop, recovery | I2 (its task 1 declares I1) | now | Ready: plan issue open |
| api-foundation | `plan/stage-1-api-foundation` | App factory and lifespan, services container, router discovery, error format, one HTTP client library (`integrations/github` moves from `httpx` to `httpx2`), login for both run modes, Host/Origin/CSRF checks, SPA serving, CLI (`start`, `backup`), committed OpenAPI contract and its CI check | none | now | Ready: plan issue open |
| api-resources | `plan/stage-1-api-resources` | REST routers for projects and the folder browser, GitHub, providers and models, agents and their settings, app info and backup | I3; I2 for the agents router | after api-foundation task 1 | Ready: plan issue open |
| api-sessions | `plan/stage-1-api-sessions` | Session REST routes and the SSE stream, two tabs and reconnects; `DELETE /api/projects/{id}` closes the project's sessions first (one line in api-resources' projects router) | I1 (contract, then the `SessionManager` class), I3, I2 and api-resources task 4 (it adds `services.agent_settings`, which `SessionManager` needs) | coding: after agent-runtime task 1 and api-foundation task 1; merging the routes: after `SessionManager` and api-resources task 4 are on `main` | Ready: plan issue open |
| frontend-foundation | `plan/stage-1-frontend-foundation` | Page registration and app shell (sidebar, gear menu), typed API client from the contract, query setup, error text, sign-in pages | I3 for the client, I4 for sign-in | now (task 1: registration and shell); the client after api-foundation task 1; sign-in after its login task | Ready: plan issue open |
| frontend-workspace | `plan/stage-1-frontend-workspace` | Projects, the project switcher in the shell's sidebar, Add project (local folder browser, GitHub URL or repo picker), project Settings (repo info, refresh, relink, model override, limits) | I5, I6 | after frontend-foundation's client task and the I6 routes it uses | Ready: plan issue open |
| frontend-providers | `plan/stage-1-frontend-providers` | AI providers (keys, detect keys, models, verify, default model per agent), Integrations (GitHub), app Settings | I5, I6 | after frontend-foundation's client task and the I6 routes it uses | Ready: plan issue open |
| frontend-chat | `plan/stage-1-frontend-chat` | Agents page: Orchestrator session, streamed answers, collapsible tool steps, queued messages, Stop, interrupted turns, reconnect | I5, I7 | coding: after frontend-foundation's client task (I7's shapes are fixed here); merging: after I7 is on `main` | Ready: plan issue open |
| packaging | `plan/stage-1-packaging` | `scripts/build_wheel.py`, wheel contents check, install check on Windows and Linux in CI | api-foundation task 4 for the install check | now | Ready: plan issue open |
| e2e | `plan/stage-1-e2e` | The Playwright test of the stage 1 flow below, through the installed app and the fake provider | every plan above | after frontend-workspace, frontend-providers and frontend-chat | Ready: plan issue open |
| test-stability | `plan/stage-1-test-stability` | Fixes the intermittent test failure #109 (executor cancel and the write lock; #110 was closed by Decision #120 as an environment limit); moves the agent-runtime session tests onto api-sessions' hold gate and removes their remaining timing assumptions and copied helpers | none; task 3 needs the hold gate (api-sessions task 2) | now; task 3 after api-sessions' plan PR (#107) | Ready: plan issue open |
| fixes | `plan/stage-1-fixes` | Small review defects: #119 (api-sessions live test timing assertion and inline suppressions), #121 (board moves a closed task when a Decision closes), #117 (repeated project queries), #125 (agents page reconnect, copied test setup) | none | now | Ready: plan issue open |

The five plans written first include full implementation code. They were written before the "How plans are written" rule in `AGENTS.md` and are kept as they are. The plans still to write follow that rule: tests as code or as behaviors to verify, full code only where a mistake is costly, at most 5 tasks, one PR per plan plus early PRs for tasks other plans consume (named in the task issues).

## Known stage 1 limitations

- A project's step-limit override cannot be removed: I2 has no call for it, and the view does not say whether the value is an override. After overriding, the user sets the number again to match the definition.

- Refreshing a GitHub project (on open or on demand) while its agent is in a turn resets the managed clone under the agent, so that turn may read a mix of old and new files. Stage 2's per-task worktrees remove this.

## Stage 1 end-to-end flow

The page plans must make this flow possible through the UI alone; the e2e plan tests it with Playwright, using visible labels (no test ids):
1. open `/#token=<token>`: the shell appears and the address bar has no token;
2. AI providers: add a self-hosted provider with the fake server's base URL; add the model `fake-tools`; verify it (verified, tool calling and streaming); make it the Orchestrator's default model;
3. Add project: a local folder holding a small committed git repository; the project opens;
4. Agents: ask "What does the README say?"; a Read tool step on `README.md` appears and expands; the answer streams in and contains the README's first line;
5. reload: the conversation is still there, tool step included.

Notes for the e2e plan: the fake server gets a `__main__` entry (`cd backend && uv run python -m tests.fakes.fake_openai --port N`); `frontend/e2e/*.ts` and `playwright.config.ts` must be in a tsconfig, or ESLint's type-aware lint fails; CI installs browsers with `npx playwright install --with-deps chromium`.

## Interfaces between plans

Names, paths and types here are fixed; everything else inside a plan is the plan's own business. Producing plans deliver these in their first tasks.

**I1. Agent sessions** (agent-runtime, task 1: the types and the protocol only; the implementation follows in later tasks). In `mlops_agents/agents/runtime/contract.py`:

```python
AuthorKind = Literal["user", "agent", "system"]
MessageStatus = Literal["complete", "streaming", "queued", "stopped", "interrupted"]
TurnEnd = Literal["done", "stopped", "interrupted", "failed"]

class TextPart(BaseModel): type: Literal["text"]; text: str
class ToolCallPart(BaseModel): type: Literal["tool_call"]; id: str; name: str; input: dict[str, Any]
class ToolResultPart(BaseModel): type: Literal["tool_result"]; tool_call_id: str; content: str; is_error: bool
Part = Annotated[TextPart | ToolCallPart | ToolResultPart, Field(discriminator="type")]

class MessageView(BaseModel):
    id: str; turn_id: str | None; author_kind: AuthorKind; author_name: str
    from_agent: str | None; parts: list[Part]; status: MessageStatus; created_at: str

class SessionView(BaseModel):
    id: str; agent: str
    busy: bool                               # a turn is running or queued
    messages: list[MessageView]              # full history, oldest first
    last_seq: int                            # stream position this view matches

# One class per event, each `seq: int` (per session, increasing; sent as the SSE id), a literal
# `type` and a typed `data` (#89); the wire shape is `{"seq", "type", "data"}`:
#   MessageCreated  "message.created"  data: MessageData {message: MessageView}   every new message, the user's included
#   TextDelta       "text.delta"       data: TextDeltaData {message_id, text}
#   ToolStarted     "tool.started"     data: ToolStartedData {message_id, tool_call_id, name, input}
#   ToolFinished    "tool.finished"    data: ToolFinishedData {message_id, tool_call_id, content, is_error}
#   MessageUpdated  "message.updated"  data: MessageData                           a message's new final state
#   TurnEnded       "turn.ended"       data: TurnEndedData {turn_id, status: TurnEnd, error: str | None}
#   Resync          "resync"           data: ResyncData {} (no fields)
StreamEvent = Annotated[MessageCreated | TextDelta | ToolStarted | ToolFinished | MessageUpdated
                        | TurnEnded | Resync, Field(discriminator="type")]
# The stream route's OpenAPI response names StreamEvent, so the frontend generates these types
# instead of writing them (api-sessions task 1).

class AgentSessions(Protocol):
    async def get(self, project_id: str, agent: str) -> SessionView: ...
    async def send(self, project_id: str, agent: str, text: str) -> MessageView: ...
    async def stop(self, project_id: str, agent: str) -> None: ...
    def subscribe(self, project_id: str, agent: str, after_seq: int | None) -> AsyncGenerator[StreamEvent, None]: ...
    # An async generator so the caller can aclose() it: closing it unsubscribes from the hub at once,
    # not when the garbage collector finalizes it (the stream route closes it when the client leaves).
    async def close_project(self, project_id: str) -> None: ...
        # before a project is removed: ends its running turns (interrupted), drops its hubs and
        # subscribers, closes its database handles; later calls for that project start fresh
```
Errors are `DomainError`s: unknown agent 404, an agent without chat 409, model problems from I2 as they come.

Two tabs and reconnects: a client calls `get`, then `subscribe(after_seq=last_seq)`. `get` returns a message that is still streaming with all the text and tool steps produced so far, so a tab opened mid-turn misses nothing, and the events after `last_seq` carry only what came later. When `after_seq` is older than the hub's in-memory buffer, or newer than the session's current seq (the process restarted, which resets seq), `subscribe` first yields one event of type `resync` with empty `data`, and the client calls `get` again. So `StreamEvent` also includes `Resync`.

The implementation is `mlops_agents.agents.runtime.sessions.SessionManager`, which implements `AgentSessions`. Its constructor is fixed by agent-runtime task 1 in `contract.py`'s docstring, and agent-runtime's task list names the task that delivers it. api-sessions and frontend-chat code against the contract from task 1; their routes and page merge after `SessionManager` is on `main`.

**I2. Agent settings** (agent-settings, task 1). In `mlops_agents/agents/settings.py`:

```python
class AgentLimitsUpdate(BaseModel): max_steps: int | None = Field(ge=1)   # None = no limit

class AgentSettingsView(BaseModel):
    agent: str
    display_name: str                        # from the definition, for labels in the UI
    default_model: str | None                # app level
    project_model: str | None                # project override (None outside a project)
    effective_model: str | None
    model_problem: str | None                # plain text, e.g. the model no longer exists
    max_steps: int | None                    # effective: project override, else the definition's

class AgentSettingsService:
    def __init__(self, databases: Databases, providers: ProvidersService, catalog: AgentCatalog) -> None: ...
    async def get(self, agent: str, project_id: str | None = None) -> AgentSettingsView: ...
    async def list_settings(self, project_id: str | None = None) -> list[AgentSettingsView]: ...
    async def set_default_model(self, agent: str, address: str | None) -> AgentSettingsView: ...
    async def set_project_model(self, project_id: str, agent: str, address: str | None) -> AgentSettingsView: ...
    async def set_project_limits(self, project_id: str, agent: str, limits: AgentLimitsUpdate) -> AgentSettingsView: ...
    async def resolve(self, project_id: str, agent: str) -> tuple[ResolvedModel, int | None]: ...
```
`resolve` returns the model and `max_steps` for a turn. It raises `DomainError` (409) with a message saying what to choose when no model is assigned, the model no longer exists, or the model has a `ModelEntry.problem` (I6, the rule's one source), whose text it uses as the message.

**I3. API skeleton** (api-foundation, task 1):
- `mlops_agents/api/app.py`: `create_app(settings: Settings, *, keystore: KeyStore | None = None, gh: GhLike | None = None) -> FastAPI` (the CLI passes the key store it opened; tests pass a memory key store and a missing `gh`). Its lifespan builds the services inside one `AsyncExitStack`: a service registers its own startup work and cleanup where it is built.
- `mlops_agents/api/services.py`: `@dataclass class Services` with `settings`, `paths`, `databases`, `keystore`, `providers: ProvidersService`, `github: GitHubAuth`, `workspace: WorkspaceService`, `catalog: AgentCatalog`, `tools: ToolRegistry`. api-resources adds `agent_settings: AgentSettingsService` and api-sessions adds `sessions: AgentSessions`, each as one field and one line in the builder.
- `mlops_agents/api/deps.py`: `ServicesDep = Annotated[Services, Depends(get_services)]`; `ProjectDep`, the `Project` named by the `project_id` path parameter (404 if unknown).
- Routers are discovered: every module in `mlops_agents/api/routers/` that defines `router: APIRouter` is included under `/api`. Adding a router is adding a file; nobody edits a list.
- Every error response is `{"code": str, "message": str, "detail": dict | null}` with the `DomainError`'s status. Validation errors are 422 with code `invalid_request` and `detail.errors` = `[{field, message}]`, where `message` is the validator's own sentence (pydantic's "Value error, " prefix removed). `KeyStoreError` is 503 with code `keystore_unavailable` and its own message.
- Access is checked app-wide by the skeleton, never per router: every `/api` route except `/api/auth/*` refuses a request without a valid session (401, code `not_signed_in`), and every state-changing request without the CSRF header (403, code `csrf_failed`). Task 1 enforces it with the session check the login task then fills in; a router merged before that is still closed.
- `backend/tests/api/conftest.py` provides `make_client(mode="local" | "server", **settings_env)`, an async context manager yielding a signed-in client for a new started app with its own data folder (it gives no handle on the app), plus `server_client` (built with it) (server mode: a password, a master key, `base_url="https://app.example.com"` with `app.example.com` in `allowed_hosts`, a memory key store, signed in; `https` because server-mode cookies are `Secure` and the Origin check expects `https`). It provides `client`: an async HTTP client (`get`, `post`, `put`, `delete` returning responses with `status_code` and `json()`) calling the `app` fixture (the started app, where a test registers its own routes) in process with a temporary data folder, signed in, sending the CSRF header; `anonymous` calls the same app without signing in. Every router's tests use it; nobody adds another signed-in setup. That client runs the app in process and cannot read a stream, so the file also provides `live_client`: the `live_app` fixture (a separate app, where stream tests register their routes) served by uvicorn on a free local port, with the same sign-in, for SSE tests (api-sessions).
- Request and response models get unique, resource-prefixed class names (`AddLocalProject`, `RelinkProject`, `AddProviderKey`, `VerifyModel`, `SetContextWindow`, `SetAgentModel`, `SendMessage`, ...). FastAPI names OpenAPI components after the class; two routers with the same class name produce `mlops_agents__api__...` names that change the generated TypeScript whenever another router is added. `test_openapi.py` checks no component name contains `__`.
- `frontend/openapi.json` is exported by `uv run python -m mlops_agents.api.openapi_export` and checked in CI. After a merge it is regenerated, never merged by hand.

**I4. Sign-in** (api-foundation, the login task): `GET /api/auth/status` → `{"mode": "local" | "server", "signed_in": bool}`; `POST /api/auth/token` `{"token"}` (local mode); `POST /api/auth/login` `{"password"}` (server mode); `POST /api/auth/logout`. In local mode the app opens `http://127.0.0.1:<port>/#token=<token>` (`http://localhost:5173/#token=<token>` with `start --dev`); the UI posts that token to `/api/auth/token` and removes it from the address bar. A signed-in browser holds an HttpOnly session cookie; state-changing requests send the value of the `csrf_token` cookie in an `X-CSRF-Token` header.

**I5. Frontend foundation** (frontend-foundation, task 1):
- `frontend/src/lib/api.ts` (frontend-foundation task 2): `createApiClient<P>(fetchImpl?)` and `api = createApiClient<paths>()`, an `openapi-fetch` client typed from `frontend/src/api/schema.d.ts` (generated from `openapi.json` by `npm run gen:api`) that sends the CSRF header; `ApiError` with `code`, `status`, `detail`; `unwrap(result)` (returns `data` or throws `ApiError`); `errorText(error: unknown): string`; `fieldErrors(error: unknown): Record<string, string>` (from a 422's `detail.errors`, keyed by the field name without the `body.` prefix, for forms); `src/lib/query.ts`: `queryClient`. `src/api/schema.d.ts` is generated by npm pre-scripts and never committed.
- Pages register themselves (frontend-foundation task 1, `src/app/routes.ts`): every `frontend/src/pages/**/route.tsx` exports `route: PageRoute`, collected with `import.meta.glob`. A page shows in the navigation only through its own `nav`, so a page and its menu entry merge together.
  ```ts
  type PageRoute = {
    path: string;                    // "/providers", "/projects/:projectId/agents"
    element: React.ReactNode;
    nav?: { area: "app" | "project"; label: string; order: number; icon?: LucideIcon };
  };
  ```

- `frontend/src/lib/models.ts` (frontend-foundation task 2): `hostedModelWarning(provider)`, used by every page that assigns a model. Whether a model is usable is not decided here: a page reads `ModelEntry.problem` (I6), None when usable (#88).
- Page paths, fixed so pages can link to each other: `/` Projects (frontend-workspace, app nav order 0), `/projects/new` Add project, `/projects/:projectId` (redirects to the first project page by `order`), `/projects/:projectId/agents` Agents (frontend-chat, project nav order 0), `/projects/:projectId/settings` Project settings (frontend-workspace, project nav order 100), `/providers` AI providers (frontend-providers, app nav order 10), `/integrations` Integrations (order 20), `/settings` App settings (order 30). Nav labels are unique, so role and label queries (e2e) are never ambiguous.
- Tests render pages with `src/test/render.tsx`: `renderPage(path: string, options?: { routes?: PageRoute[] })` renders the real router (memory history at `path`) with a fresh `QueryClient`; requests are answered by `vi.stubGlobal("fetch", ...)`, which works because `createApiClient`'s default fetch calls `globalThis.fetch` at request time.

**I6. Resource API** (api-resources). Bodies are the merged pydantic models (`Project`, `FolderListing`, `Provider`, `ModelEntry` (with the computed `problem: str | None`, the one source of the usable-model rule, #88), `RemoteModel`, `DetectedKey`, `GitHubStatus`, `RepoSummary`) and I2's `AgentSettingsView`; the OpenAPI contract is the reference.
- Projects: `GET/POST /api/projects` (POST body `{"source": "local", "path", "init_git"}` or `{"source": "github", "repo"}`), `GET/DELETE /api/projects/{project_id}`, `POST .../open`, `POST .../refresh`, `POST .../relink` `{"path"}`, `GET /api/folders?path=` (local mode only).
- GitHub: `GET /api/github`, `POST /api/github/gh`, `POST /api/github/token` `{"token"}`, `DELETE /api/github`, `GET /api/github/repos`.
- Providers: `GET/POST /api/providers`, `DELETE /api/providers/{name}`, `POST /api/providers/{name}/keys` `{"key"}`, `DELETE /api/providers/{name}/keys/{position}`, `GET /api/providers/{name}/remote-models`, `GET /api/env-keys`, `POST /api/env-keys/{env_var}/import`.
- Models: wire model IDs contain `/` (`openai/gpt-oss-120b`, `models/gemini-…`), so an address never goes in the path: `GET/POST /api/models`, `DELETE /api/models?address=`, `POST /api/models/verify` `{"address"}`, `PUT /api/models/context-window` `{"address", "value"}`.
- Agents: `AgentInfo {name, display_name, description, chat, tools, settings: AgentSettingsView}`. `GET /api/agents` (app-level settings), `PUT /api/agents/{agent}/model` `{"address"}` (`null` clears it); `GET /api/projects/{project_id}/agents`, `PUT /api/projects/{project_id}/agents/{agent}/model` `{"address"}`, `PUT /api/projects/{project_id}/agents/{agent}/limits` `{"max_steps"}`.
- App: `GET /api/app` → `{"name", "version", "mode", "data_dir"}`; `POST /api/app/backup` → `{"path"}`.

**I7. Session API** (api-sessions, task 1): `GET /api/projects/{project_id}/agents/{agent}/session` → `SessionView`; `POST .../session/messages` `{"text"}` → `MessageView`; `POST .../session/stop`; `GET .../session/stream` (SSE: each event's `id` is `StreamEvent.seq`, `event` its `type`, `data` its JSON `data`; `Last-Event-ID` resumes after that seq).

**Hold gate** (api-sessions, task 2; test code, consumed by test-stability task 3): `FakeState.hold_slow() -> None` and `FakeState.release_slow() -> None` in `backend/tests/fakes/fake_openai.py`. Open by default; `reset()` and the `fake_openai` fixture's teardown release it.

## Global Constraints

Every task in every stage 1 plan includes these:

- **Runtimes and backend versions:**
  - Python `>=3.12`; Node 22.
  - Backend dependency floors are the versions verified on 2026-10-16: langgraph 1.2.15, langgraph-checkpoint-sqlite 3.1.2, langchain-openai 1.7.2, langchain-core 1.6.11, fastapi 0.143.2, uvicorn 0.44.0, sqlalchemy 2.1.4, aiosqlite 0.22.1, pydantic 2.13.1, cryptography 46.0.7, python-ulid 4.0.1, platformdirs 4.9.6, sse-starlette 3.3.4, httpx 0.28.1, httpx2 2.13.1, keyring 25.7.0.
  - The `openai` SDK (through langchain-openai) runs on `httpx2`, not `httpx`. Model calls go through one app-owned `httpx2.AsyncClient` (providers plan); never langchain-openai's default client, which is cached for the whole process and bound to the first event loop.
- **Frontend versions:**
  - Majors are pinned to the versions this code is written against: react 19, vite 7, react-router 7, typescript ~5.9, tailwindcss 4, vitest 5, eslint 9, @tanstack/react-query 5, openapi-typescript 7, openapi-fetch 0.17.
  - Newer majors exist (vite 8, react-router 8, typescript 7, eslint 10). Upgrading them is a separate, explicit decision. Vitest went from 3 to 5 on 2026-10-16 by such a decision: vitest 3 depended on packages with critical advisories that no 3.x release fixes.
  - shadcn/ui uses the `radix-vega` style with the neutral base color. shadcn 4 no longer has the `new-york` style.
- **App name:** it appears only in `backend/src/mlops_agents/config.py` (`APP_NAME`), `backend/pyproject.toml` (the project name and the script) and `frontend/src/config.ts` (`APP_NAME`, the UI's single copy). Everything else imports `APP_NAME`.
- **IDs and time:** IDs come only from `mlops_agents.ids.new_id(prefix)`, with prefixes registered in `docs/roadmap.md`. Timestamps come only from `mlops_agents.clock.now_iso()` (UTC ISO 8601).
- **SQLite:** every connection (SQLAlchemy, sqlite3, the LangGraph checkpointer) applies `mlops_agents.storage.sqlite.PRAGMAS`.
- **Secrets:** no key, token or password ever appears in a database row, URL, log line, error message or test snapshot. Errors that may contain one go through `mlops_agents.credentials.scrub`.
- **Blocking work and subprocesses:**
  - File reads, `git`, `gh`, `rg`, keyring and scrypt never run on the event loop; use `asyncio.to_thread`.
  - Subprocesses run via `subprocess.run` inside `asyncio.to_thread`, which works on every Windows event loop. Never use `asyncio.create_subprocess_exec`.
- **Checks:**
  - Python: ruff and pyright (strict) pass. Inline suppressions are allowed only at a third-party boundary, and the suppression names the rule and says why.
  - TypeScript: `strict` mode, ESLint and Prettier pass. No `any`.
- **Error messages:** every error a user can see says what happened and what to do, in plain words.

## Review Focus

These are the inputs most likely to hurt a real user without any single task catching them. The owning plan carries the test for each.

1. **Windows paths.** Backslashes, drive-letter case and mixed case in a local project path must still detect duplicates and resolve tracked files. Tests: github-workspace and agent-tools.
2. **Huge, binary or non-UTF-8 files.** Read and Grep must answer quickly with a clear note, and must never crash or flood the context. Tests: agent-tools.
3. **Unusual provider streams.** Some providers split tool-call arguments across stream chunks, or report no token usage while streaming. Verification and turns must still work. Tests: providers and agent-runtime.
4. **Two tabs, or a reconnect mid-turn.** With two browser tabs on the same session, or a reconnect in the middle of a turn, both tabs show the same answer with no missing or duplicated text. Tests: agent-runtime and api-sessions.
5. **A GitHub token that expires or is revoked after a project was added.** Refresh fails with a clear message, and the last good copy stays readable. Tests: github-workspace.

## File structure

```
.github/workflows/      backend.yml, frontend.yml; later plans add contract, e2e and package checks
AGENTS.md               gains Backend commands and Frontend commands sections
scripts/build_wheel.py  builds the UI into the backend, then the wheel (packaging plan)
backend/
  pyproject.toml, uv.lock
  src/mlops_agents/
    config.py, paths.py, ids.py, clock.py, errors.py
    storage/            sqlite.py, migrate.py, backup.py, databases.py, tables.py, migrations/
    credentials/        keystore.py, keyring_store.py, file_store.py, scrub.py
    providers/          models.py, service.py, errors.py, bridge.py, verification.py
    integrations/github/ git.py, api.py, auth.py
    workspace/          models.py, refs.py, service.py
    agents/
      catalog.py, settings.py (I2), definitions/orchestrator.md
      tools/            base.py, files.py, read.py, glob.py, grep.py
      runtime/          contract.py (I1); the rest is the agent-runtime plan's
    api/                app.py, services.py, deps.py, errors.py, openapi_export.py (I3),
                        routers/ (one file per router), and the api-foundation plan's own files
    cli.py
    web/                built UI (generated, gitignored)
  tests/                mirrors src; tests/fakes/fake_openai.py is a fake OpenAI-compatible server
frontend/
  openapi.json          committed API contract, generated by the backend
  src/lib/api.ts, src/api/schema.d.ts (generated) (I5)
  src/pages/<page>/route.tsx and the page's files, one folder per page (I5)
  e2e/                  Playwright test (packaging plan)
```

`credentials/` is a top-level module because both providers and GitHub use it. (The spec groups the keystore under providers.) The spec's behavior is unchanged.
