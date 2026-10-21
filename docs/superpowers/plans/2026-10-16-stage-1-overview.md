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
| agent-runtime | `plan/stage-1-agent-runtime` | Message store, context and compaction, model calls with retries and key rotation, the LangGraph loop, turn executor, stream hub, session queue, Stop, recovery | I2 (its task 1 declares I1) | now | To write |
| api-foundation | `plan/stage-1-api-foundation` | App factory and lifespan, services container, router discovery, error format, one HTTP client library (`integrations/github` moves from `httpx` to `httpx2`), login for both run modes, Host/Origin/CSRF checks, SPA serving, CLI (`start`, `backup`), committed OpenAPI contract and its CI check | none | now | Ready: plan issue open |
| api-resources | `plan/stage-1-api-resources` | REST routers for projects and the folder browser, GitHub, providers and models, agents and their settings, app info and backup | I3; I2 for the agents router | after api-foundation task 1 | To write |
| api-sessions | `plan/stage-1-api-sessions` | Session REST routes and the SSE stream, two tabs and reconnects | I1 (contract, then the `SessionManager` class), I3 | coding: after agent-runtime task 1 and api-foundation task 1; merging the routes: after `SessionManager` is on `main` | To write |
| frontend-foundation | `plan/stage-1-frontend-foundation` | Page registration and app shell (sidebar, project switcher, gear), typed API client from the contract, query setup, error text, sign-in pages | I3 for the client, I4 for sign-in | now (task 1: registration and shell); the client after api-foundation task 1; sign-in after its login task | To write |
| frontend-workspace | `plan/stage-1-frontend-workspace` | Projects, Add project (local folder browser, GitHub URL or repo picker), project Settings (repo info, refresh, relink, model override, limits) | I5, I6 | after frontend-foundation's client task and the I6 routes it uses | To write |
| frontend-providers | `plan/stage-1-frontend-providers` | AI providers (keys, detect keys, models, verify, default model per agent), Integrations (GitHub), app Settings | I5, I6 | after frontend-foundation's client task and the I6 routes it uses | To write |
| frontend-chat | `plan/stage-1-frontend-chat` | Agents page: Orchestrator session, streamed answers, collapsible tool steps, queued messages, Stop, interrupted turns, reconnect | I5, I7 | coding: after frontend-foundation's client task (I7's shapes are fixed here); merging: after I7 is on `main` | To write |
| packaging | `plan/stage-1-packaging` | `scripts/build.py`, wheel contents check, install check on Windows and Linux in CI; last, the end-to-end Playwright test with the fake provider | the end-to-end task: every plan above | now | To write |

The five plans written first include full implementation code. They were written before the "How plans are written" rule in `AGENTS.md` and are kept as they are. The plans still to write follow that rule: tests as code or as behaviors to verify, full code only where a mistake is costly, at most 5 tasks, one PR per task.

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

class StreamEvent(BaseModel):
    seq: int                                 # per session, increasing; sent as the SSE id
    type: Literal["message.created", "text.delta", "tool.started", "tool.finished",
                  "message.updated", "turn.ended", "resync"]
    data: dict[str, Any]
    # message.created  {"message": MessageView}       every new message, the user's included
    # text.delta       {"message_id": str, "text": str}
    # tool.started     {"message_id": str, "tool_call_id": str, "name": str, "input": dict}
    # tool.finished    {"message_id": str, "tool_call_id": str, "content": str, "is_error": bool}
    # message.updated  {"message": MessageView}       a message's new final state
    # turn.ended       {"turn_id": str, "status": TurnEnd, "error": str | None}

class AgentSessions(Protocol):
    async def get(self, project_id: str, agent: str) -> SessionView: ...
    async def send(self, project_id: str, agent: str, text: str) -> MessageView: ...
    async def stop(self, project_id: str, agent: str) -> None: ...
    def subscribe(self, project_id: str, agent: str, after_seq: int | None) -> AsyncIterator[StreamEvent]: ...
```
Errors are `DomainError`s: unknown agent 404, an agent without chat 409, model problems from I2 as they come.

Two tabs and reconnects: a client calls `get`, then `subscribe(after_seq=last_seq)`. `get` returns a message that is still streaming with all the text and tool steps produced so far, so a tab opened mid-turn misses nothing, and the events after `last_seq` carry only what came later. When `after_seq` is older than the hub's in-memory buffer, or newer than the session's current seq (the process restarted, which resets seq), `subscribe` first yields one event of type `resync` with empty `data`, and the client calls `get` again. So `StreamEvent.type` also includes `"resync"`.

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
`resolve` returns the model and `max_steps` for a turn. It raises `DomainError` (409) with a message saying what to choose when no model is assigned, the model no longer exists, or it lacks verified `tool_calling`, `streaming` or a context window.

**I3. API skeleton** (api-foundation, task 1):
- `mlops_agents/api/app.py`: `create_app(settings: Settings, *, keystore: KeyStore | None = None, gh: GhLike | None = None) -> FastAPI` (the CLI passes the key store it opened; tests pass a memory key store and a missing `gh`). Its lifespan builds the services inside one `AsyncExitStack`: a service registers its own startup work and cleanup where it is built.
- `mlops_agents/api/services.py`: `@dataclass class Services` with `settings`, `paths`, `databases`, `keystore`, `providers: ProvidersService`, `github: GitHubAuth`, `workspace: WorkspaceService`, `catalog: AgentCatalog`, `tools: ToolRegistry`. api-resources adds `agent_settings: AgentSettingsService` and api-sessions adds `sessions: AgentSessions`, each as one field and one line in the builder.
- `mlops_agents/api/deps.py`: `ServicesDep = Annotated[Services, Depends(get_services)]`; `ProjectDep`, the `Project` named by the `project_id` path parameter (404 if unknown).
- Routers are discovered: every module in `mlops_agents/api/routers/` that defines `router: APIRouter` is included under `/api`. Adding a router is adding a file; nobody edits a list.
- Every error response is `{"code": str, "message": str, "detail": dict | null}` with the `DomainError`'s status. Validation errors are 422 with code `invalid_request`. `KeyStoreError` is 503 with code `keystore_unavailable` and its own message.
- Access is checked app-wide by the skeleton, never per router: every `/api` route except `/api/auth/*` refuses a request without a valid session (401, code `not_signed_in`), and every state-changing request without the CSRF header (403, code `csrf_failed`). Task 1 enforces it with the session check the login task then fills in; a router merged before that is still closed.
- `backend/tests/api/conftest.py` provides `client`: an async HTTP client (`get`, `post`, `put`, `delete` returning responses with `status_code` and `json()`) calling `create_app` in process with a temporary data folder, signed in, sending the CSRF header. Every router's tests use it; nobody adds another signed-in setup. That client runs the app in process and cannot read a stream, so the file also provides `live_client`: the app served by uvicorn on a free local port, with the same sign-in, for SSE tests (api-sessions).
- `frontend/openapi.json` is exported by `uv run python -m mlops_agents.api.openapi_export` and checked in CI. After a merge it is regenerated, never merged by hand.

**I4. Sign-in** (api-foundation, the login task): `GET /api/auth/status` → `{"mode": "local" | "server", "signed_in": bool}`; `POST /api/auth/token` `{"token"}` (local mode); `POST /api/auth/login` `{"password"}` (server mode); `POST /api/auth/logout`. In local mode the app opens `http://127.0.0.1:<port>/#token=<token>` (`http://localhost:5173/#token=<token>` with `start --dev`); the UI posts that token to `/api/auth/token` and removes it from the address bar. A signed-in browser holds an HttpOnly session cookie; state-changing requests send the value of the `csrf_token` cookie in an `X-CSRF-Token` header.

**I5. Frontend foundation** (frontend-foundation, task 1):
- `frontend/src/lib/api.ts`: `api`, an `openapi-fetch` client typed from `frontend/src/api/schema.d.ts` (generated from `openapi.json` by `npm run gen:api`) that sends the CSRF header; `ApiError` with `code`, `message`, `detail`; `errorText(error: unknown): string`.
- Pages register themselves: every `frontend/src/pages/**/route.tsx` exports `route: PageRoute`, collected with `import.meta.glob`. A page shows in the navigation only through its own `nav`, so a page and its menu entry merge together.
  ```ts
  type PageRoute = {
    path: string;                    // "/providers", "/projects/:projectId/agents"
    element: React.ReactNode;
    nav?: { area: "app" | "project"; label: string; order: number };
  };
  ```

**I6. Resource API** (api-resources). Bodies are the merged pydantic models (`Project`, `FolderListing`, `Provider`, `ModelEntry`, `RemoteModel`, `DetectedKey`, `GitHubStatus`, `RepoSummary`) and I2's `AgentSettingsView`; the OpenAPI contract is the reference.
- Projects: `GET/POST /api/projects` (POST body `{"source": "local", "path", "init_git"}` or `{"source": "github", "repo"}`), `GET/DELETE /api/projects/{project_id}`, `POST .../open`, `POST .../refresh`, `POST .../relink` `{"path"}`, `GET /api/folders?path=` (local mode only).
- GitHub: `GET /api/github`, `POST /api/github/gh`, `POST /api/github/token` `{"token"}`, `DELETE /api/github`, `GET /api/github/repos`.
- Providers: `GET/POST /api/providers`, `DELETE /api/providers/{name}`, `POST /api/providers/{name}/keys` `{"key"}`, `DELETE /api/providers/{name}/keys/{position}`, `GET /api/providers/{name}/remote-models`, `GET /api/env-keys`, `POST /api/env-keys/{env_var}/import`.
- Models: wire model IDs contain `/` (`openai/gpt-oss-120b`, `models/gemini-…`), so an address never goes in the path: `GET/POST /api/models`, `DELETE /api/models?address=`, `POST /api/models/verify` `{"address"}`, `PUT /api/models/context-window` `{"address", "value"}`.
- Agents: `GET /api/agents` (catalog with app-level settings), `PUT /api/agents/{agent}/model` `{"address"}` (`null` clears it); `GET /api/projects/{project_id}/agents`, `PUT /api/projects/{project_id}/agents/{agent}/model` `{"address"}`, `PUT /api/projects/{project_id}/agents/{agent}/limits` `{"max_steps"}`.
- App: `GET /api/app` → `{"name", "version", "mode", "data_dir"}`; `POST /api/app/backup` → `{"path"}`.

**I7. Session API** (api-sessions, task 1): `GET /api/projects/{project_id}/agents/{agent}/session` → `SessionView`; `POST .../session/messages` `{"text"}` → `MessageView`; `POST .../session/stop`; `GET .../session/stream` (SSE: each event's `id` is `StreamEvent.seq`, `event` its `type`, `data` its JSON `data`; `Last-Event-ID` resumes after that seq).

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
scripts/build.py        builds the UI into the backend, then the wheel (packaging plan)
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
