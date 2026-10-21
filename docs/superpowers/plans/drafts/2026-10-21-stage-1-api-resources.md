> **DRAFT. Do not implement.** Still missing: an advisor review round with no blocking issues. The lead removes this banner when the plan leaves `drafts/`.

# Stage 1 — API Resources Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. Each task is one PR into `main` (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** The REST routes the stage 1 pages use: projects and the folder browser, app info and backup, GitHub, AI providers and models, and agents with their settings. Thin routers over the merged services: they validate input, call one service method, and return the service's models.

**Branch:** `plan/stage-1-api-resources`. **Consumes:** I3 (api-foundation task 1) for every task; I2 (agent-settings) for task 4. **Produces:** I6 of the stage overview.

**Read first:** the stage overview (global constraints, "Interfaces between plans" I2, I3, I6, and the stage 1 end-to-end flow), spec sections 2 ("Workspace operations"), 3, 4 and 6, and the merged services each task wraps.

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | Projects, folder browser, app info and backup | I3 on `main` |
| 2 | GitHub | I3 on `main` |
| 3 | AI providers, models and environment keys | I3 on `main` |
| 4 | Agents and their settings | I3 and I2 on `main` |

The four tasks touch different files and can be done in any order. Do them in the order frontend-workspace and frontend-providers need them: 1, 3, 2, 4.

## Rules for every router

- One file per router in `backend/src/mlops_agents/api/routers/`, defining `router = APIRouter(tags=[...])` (I3 discovers it). Paths are written without the `/api` prefix, which discovery adds.
- Every handler takes `ServicesDep` (and `ProjectDep` for `/projects/{project_id}/...`), calls one service method, and returns its result. No business rules in routers: a rule missing from a service is added to the service, with a test there.
- Request bodies are small pydantic models defined in the router file, with `model_config = ConfigDict(extra="forbid")` and unique, resource-prefixed names (I3): `AddLocalProject`, `AddGitHubProject`, `RelinkProject`, `SetGitHubToken`, `AddProviderKey`, `ModelAddress`, `VerifyModel`, `SetContextWindow`, `SetAgentModel`. Responses use `response_model` with the merged models, so the OpenAPI contract names them.
- A route that returns nothing answers 204.
- Errors are the services' `DomainError`s, unchanged. Mode rules stay in the services (`WorkspaceService` already refuses local projects in server mode with 403 `local_projects_disabled`).
- Re-export `frontend/openapi.json` in every task (`uv run python -m mlops_agents.api.openapi_export`).
- Tests use I3's `client` fixture (signed in, CSRF header) and `anonymous` for one "needs sign-in" check per router. They check status codes, the response bodies' shapes and the effect through a following GET, not the services' internals (the services have their own tests).

---

### Task 1: Projects, folder browser, app info and backup

**Files:**
- Create: `backend/src/mlops_agents/api/routers/projects.py`, `backend/src/mlops_agents/api/routers/app_info.py`
- Modify: `frontend/openapi.json`
- Test: `backend/tests/api/test_projects.py`, `backend/tests/api/test_app_info.py`

**Interfaces:**
- Consumes: `WorkspaceService` (`list_projects`, `get`, `add_local`, `add_github`, `open`, `refresh`, `relink`, `remove`, `browse`), `Project`, `FolderListing`; `snapshot_all`; `APP_NAME`; I3's `ServicesDep`, `ProjectDep`.
- Produces (I6, Projects and App):
  - `GET /projects` → `list[Project]`; `POST /projects` with `AddLocalProject {source: "local", path: str, init_git: bool = False}` or `AddGitHubProject {source: "github", repo: str}` (a discriminated union on `source`) → 201 `Project`.
  - `GET /projects/{project_id}` → `Project`; `DELETE /projects/{project_id}` → 204; `POST /projects/{project_id}/open`, `/refresh` → `Project`; `POST /projects/{project_id}/relink` with `{path}` → `Project`.
  - `GET /folders?path=` → `FolderListing` (`path` optional: the home folder).
  - `GET /app` → `AppInfo {name: str, version: str, mode: "local" | "server", data_dir: str}`; `POST /app/backup` → `BackupResult {path: str}`.

**Implementation notes:**
- `version` is `importlib.metadata.version(APP_NAME)`.
- `POST /app/backup` runs `snapshot_all(services.paths)` in `asyncio.to_thread` and returns the snapshot folder.
- `DELETE /projects/{project_id}` calls `workspace.remove`. api-sessions later adds one line before it that closes the project's sessions (`await services.sessions.close_project(project_id)`, I1); leave the handler a plain two-line function so that edit is trivial.
- `data_dir` is shown on the app Settings page. It is a local path of the machine running the app, not a secret.

**Tests (behaviors to verify)** (local projects on repositories from `make_repo`; GitHub projects are covered by task 2's fake):
- Add a local repository → 201 with `source: "local"` and `status: "ok"`; it is listed; `GET` by id returns it; the same path again (different case on Windows, a trailing slash) → 409 from the service's duplicate check.
- A folder that is not a git repository → the service's error; the same with `init_git: true` → 201.
- `open` updates `last_opened_at`; `refresh` after deleting the folder → `status: "missing"`; `relink` to a new path → `ok`; `relink` on a GitHub project → 409 `not_local`.
- `DELETE` → 204, then `GET` → 404 `project_not_found`, and the project's data folder is gone while the user's repository is untouched.
- `GET /folders` with no path lists the home folder; with a repository's parent marks it `is_git_repo`.
- Server mode (I3's `server_client`): `POST /projects` with `source: "local"`, `/folders` and `relink` → 403 `local_projects_disabled`.
- A body with an unknown field or a missing `source` → 422 `invalid_request`.
- `GET /app` returns the name, the installed version, `local`, and the temporary data folder; `POST /app/backup` creates the snapshot folder it returns.
- `anonymous` `GET /projects` → 401.

**Checks:** `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`, `uv run python -m mlops_agents.api.openapi_export --check`.

---

### Task 2: GitHub

**Files:**
- Create: `backend/src/mlops_agents/api/routers/github.py`
- Modify: `backend/src/mlops_agents/integrations/github/auth.py` (`list_repos`), `frontend/openapi.json`
- Test: `backend/tests/api/test_github_routes.py`, `backend/tests/integrations/test_github_auth.py` (the new method)

**Interfaces:**
- Consumes: `GitHubAuth` (`status`, `enable_gh`, `set_token`, `disconnect`, `require_token`), `list_repos` and `RepoSummary` from `integrations/github/api.py`.
- Produces (I6, GitHub): `GET /github` → `GitHubStatus`; `POST /github/gh` → `GitHubStatus`; `POST /github/token` with `{token}` → `GitHubStatus`; `DELETE /github` → 204; `GET /github/repos` → `list[RepoSummary]`.
- Adds `GitHubAuth.list_repos(self) -> list[RepoSummary]`: `list_repos(self._http, await self.require_token())`. Routers cannot reach the HTTP client, and the service is where GitHub calls belong.

**Implementation notes:**
- The token is never echoed: `set_token` returns `GitHubStatus` (login, mode), and a validation error on the body never contains the submitted value (I3's 422 handler already omits values).

**Tests (behaviors to verify)**: the app's GitHub client must not reach github.com. After the `client` fixture's lifespan starts, replace `services.github` and `services.workspace` (through `app.state.services`, a dataclass) with instances built on an `httpx2.AsyncClient(transport=httpx2.MockTransport(handler))` whose handler answers `/user` and `/user/repos` like `tests/integrations/test_github_api.py` does. Then:
- `GET /github` with nothing connected → `mode: "none"`; `POST /github/token` with a good token → `mode: "token"` and the login; a rejected token → the service's 401-class error, and nothing is saved; the token appears in no response body.
- `GET /github/repos` → the fake's repositories; with nothing connected → 409 `github_not_connected`.
- `POST /github/gh` when `gh` is not available (the fixture's `NoGh`) → 409 `gh_not_installed`.
- Adding a GitHub project through the route: build the replacement `WorkspaceService` as `tests/workspace/test_github.py` does, with `remote_url` pointing at a local bare repository, and a new `GitRunner` whose `token_provider` is the new `GitHubAuth`'s `token` (swapping only `services.github` would leave the workspace's runner reading tokens from the old one); `POST /projects` with `{source: "github", repo: "octo/demo"}` → 201, `status: "ok"`; then `POST /projects/{id}/refresh` → `ok`; with the token revoked in the fake → the project keeps its last copy and `status_detail` says the refresh failed (review focus 5).
- `DELETE /github` → 204, then status is `none` and the key store no longer holds the token.
- `anonymous` → 401.

**Checks:** as task 1.

---

### Task 3: AI providers, models and environment keys

**Files:**
- Create: `backend/src/mlops_agents/api/routers/providers.py`, `backend/src/mlops_agents/api/routers/models.py`
- Modify: `frontend/openapi.json`
- Test: `backend/tests/api/test_providers_routes.py`, `backend/tests/api/test_models_routes.py`

**Interfaces:**
- Consumes: `ProvidersService` (every public method), `ProviderCreate`, `Provider`, `ModelCreate`, `ModelEntry`, `RemoteModel`, `DetectedKey`.
- Produces (I6, Providers and Models):
  - `GET /providers` → `list[Provider]`; `POST /providers` with `ProviderCreate` → 201 `Provider`; `DELETE /providers/{name}` → 204.
  - `POST /providers/{name}/keys` with `{key}` → `Provider`; `DELETE /providers/{name}/keys/{position}` → `Provider`.
  - `GET /providers/{name}/remote-models` → `list[RemoteModel]`.
  - `GET /env-keys` → `list[DetectedKey]`; `POST /env-keys/{env_var}/import` → `Provider`.
  - `GET /models` → `list[ModelEntry]`; `POST /models` with `ModelCreate` → 201 `ModelEntry`; `DELETE /models?address=` → 204; `POST /models/verify` with `{address}` → `ModelEntry`; `PUT /models/context-window` with `{address, value}` (`value > 0`) → `ModelEntry`.

**Implementation notes:**
- Addresses never go in the path: wire model ids contain `/` (I6).
- `GET /env-keys` reads the server's own environment (`detect_env_keys()` with no argument). In server mode that is the server's environment, which is what the user configured there; the response holds masked hints only.
- `POST /models/verify` makes a real model call and can take seconds; it is a plain request (the page shows a spinner).

**Tests (behaviors to verify)** against the fake OpenAI-compatible server (`fake_openai` fixture):
- Create a self-hosted provider with the fake's base URL → 201; a base URL with credentials in it → 422 with the validator's message and without the credentials; a duplicate name → 409.
- Add a key → `Provider` with a masked hint only; the key appears in no response body (search every response of the test); remove it by position.
- Remote models list the fake's models with their context windows.
- Add `fake-tools`, verify it → `availability: "verified"`, `tool_calling` and `streaming` true; `fake-no-tools` verifies without tool calling; set a context window; `DELETE /models?address=` a model whose wire id contains `/` (add one by typing its id) → 204.
- `GET /env-keys` with `monkeypatch.setenv("GROQ_API_KEY", ...)` lists it masked; importing it creates the provider.
- `anonymous` → 401.

**Checks:** as task 1.

---

### Task 4: Agents and their settings

**Files:**
- Create: `backend/src/mlops_agents/api/routers/agents.py`
- Modify: `backend/src/mlops_agents/api/services.py` and `app.py` (the `agent_settings: AgentSettingsService` field and its one builder line, I3), `frontend/openapi.json`
- Test: `backend/tests/api/test_agents_routes.py`

**Interfaces:**
- Consumes: I2 (`AgentSettingsService`, `AgentSettingsView`, `AgentLimitsUpdate`); `AgentCatalog`, `AgentDefinition`.
- Produces (I6, Agents):
  ```python
  class AgentInfo(BaseModel):
      name: str; display_name: str; description: str
      chat: bool                       # the chat pages list only chat agents
      tools: list[str]
      settings: AgentSettingsView      # app level on /agents, project level on /projects/{id}/agents
  ```
  `GET /agents` → `list[AgentInfo]`; `PUT /agents/{agent}/model` with `{address: str | None}` → `AgentInfo`; `GET /projects/{project_id}/agents` → `list[AgentInfo]`; `PUT /projects/{project_id}/agents/{agent}/model` with `{address: str | None}` → `AgentInfo`; `PUT /projects/{project_id}/agents/{agent}/limits` with `AgentLimitsUpdate` → `AgentInfo`.

**Implementation notes:**
- `AgentInfo` joins the definition (name, display name, description, chat, tools) and the settings view. Order: the catalog's, by name.
- Project routes use `ProjectDep`, so an unknown project is 404 before the settings service touches a database (I2 requires an existing project).

**Tests (behaviors to verify)** (models added and verified through the providers routes against the fake server):
- `GET /agents` lists the Orchestrator with `chat: true`, its tools, and `settings.model_problem` saying to choose a model; after `PUT /agents/orchestrator/model` with a verified model the problem is gone and `effective_model` is set; `{address: null}` clears it.
- A project override changes `effective_model` for that project only; limits `{max_steps: 5}` and `{max_steps: null}` are returned; `{max_steps: 0}` → 422.
- An unverified model → 409 `model_not_usable`; an unknown agent → 404 `agent_not_found`; an unknown project → 404 `project_not_found`.
- `anonymous` → 401.

**Checks:** as task 1.

---

## Not in this plan

- The session routes and stream (api-sessions), which also add the session close to `DELETE /projects/{project_id}`.
- The pages (frontend-workspace, frontend-providers).
